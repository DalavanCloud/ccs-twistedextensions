# -*- test-case-name: twext.who.ldap.test.test_service -*-
##
# Copyright (c) 2013-2016 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
LDAP directory service implementation.
"""

from __future__ import print_function

from Queue import Queue, Empty
from threading import RLock
from uuid import UUID

import collections
import ldap.async
import time

from twisted.python.constants import Names, NamedConstant
from twisted.internet.defer import succeed, inlineCallbacks, returnValue
from twisted.internet.threads import deferToThreadPool
from twisted.cred.credentials import IUsernamePassword
from twisted.python.threadpool import ThreadPool
from twisted.internet import reactor

from twext.python.log import Logger
from twext.python.types import MappingProxyType

from ..idirectory import (
    DirectoryServiceError, DirectoryAvailabilityError,
    FieldName as BaseFieldName, RecordType as BaseRecordType,
    IPlaintextPasswordVerifier, DirectoryConfigurationError
)
from ..directory import (
    DirectoryService as BaseDirectoryService,
    DirectoryRecord as BaseDirectoryRecord,
)
from ..expression import (
    MatchExpression, ExistsExpression, BooleanExpression,
    CompoundExpression, Operand, MatchType
)
from ..util import ConstantsContainer
from ._constants import LDAPAttribute, LDAPObjectClass
from ._util import (
    ldapQueryStringFromMatchExpression,
    ldapQueryStringFromCompoundExpression,
    ldapQueryStringFromBooleanExpression,
    ldapQueryStringFromExistsExpression,
)
from zope.interface import implementer


#
# Exceptions
#

class LDAPError(DirectoryServiceError):
    """
    LDAP error.
    """

    def __init__(self, message, ldapError=None):
        super(LDAPError, self).__init__(message)
        self.ldapError = ldapError


class LDAPConfigurationError(ValueError):
    """
    LDAP configuration error.
    """


class LDAPConnectionError(DirectoryAvailabilityError):
    """
    LDAP connection error.
    """

    def __init__(self, message, ldapError=None):
        super(LDAPConnectionError, self).__init__(message)
        self.ldapError = ldapError


class LDAPBindAuthError(LDAPConnectionError):
    """
    LDAP bind auth error.
    """


class LDAPQueryError(LDAPError):
    """
    LDAP query error.
    """


#
# Data type extensions
#

class FieldName(Names):
    """
    LDAP field names.
    """
    dn = NamedConstant()
    dn.description = u"distinguished name"

    memberDNs = NamedConstant()
    memberDNs.description = u"member DNs"
    memberDNs.multiValue = True


#
# LDAP schema descriptions
#

class RecordTypeSchema(object):
    """
    Describes the LDAP schema for a record type.
    """

    def __init__(self, relativeDN, attributes):
        """
        @param relativeDN: The relative distinguished name for the record type.
            This is prepended to the service's base distinguished name when
            searching for records of this type.
        @type relativeDN: L{unicode}

        @param attributes: Attribute/value pairs that are expected for records
            of this type.
        @type attributes: iterable of sequences containing two L{unicode}s
        """
        self.relativeDN = relativeDN
        self.attributes = tuple(tuple(pair) for pair in attributes)


# We use strings (constant.value) instead of constants for the values in
# these mappings because it's meant to be configurable by application users,
# and user input forms such as config files aren't going to be able to use
# the constants.

# Maps field name -> LDAP attribute names

# NOTE: you must provide a mapping for uid

DEFAULT_FIELDNAME_ATTRIBUTE_MAP = MappingProxyType({
    # FieldName.dn: (LDAPAttribute.dn.value,),
    # BaseFieldName.uid: (LDAPAttribute.dn.value,),
    BaseFieldName.guid: (LDAPAttribute.generatedUUID.value,),
    BaseFieldName.shortNames: (LDAPAttribute.uid.value,),
    BaseFieldName.fullNames: (LDAPAttribute.cn.value,),
    BaseFieldName.emailAddresses: (LDAPAttribute.mail.value,),
    BaseFieldName.password: (LDAPAttribute.userPassword.value,),
})

# Information about record types
DEFAULT_RECORDTYPE_SCHEMAS = MappingProxyType({

    BaseRecordType.user: RecordTypeSchema(
        # ou=person
        relativeDN=u"ou={0}".format(LDAPObjectClass.person.value),

        # (objectClass=inetOrgPerson)
        attributes=(
            (
                LDAPAttribute.objectClass.value,
                LDAPObjectClass.inetOrgPerson.value,
            ),
        ),
    ),

    BaseRecordType.group: RecordTypeSchema(
        # ou=groupOfNames
        relativeDN=u"ou={0}".format(LDAPObjectClass.groupOfNames.value),

        # (objectClass=groupOfNames)
        attributes=(
            (
                LDAPAttribute.objectClass.value,
                LDAPObjectClass.groupOfNames.value,
            ),
        ),
    ),

})


class ConnectionPool(object):

    log = Logger()

    def __init__(self, poolName, ds, credentials, connectionMax):
        self.poolName = poolName
        self.ds = ds
        self.credentials = credentials
        self.connectionQueue = Queue()
        self.connectionCreateLock = RLock()
        self.connections = []
        self.activeCount = 0
        self.connectionsCreated = 0
        self.connectionMax = connectionMax
        self.log.debug(
            "Created {pool} LDAP connection pool with connectionMax={max}",
            pool=self.poolName, max=self.connectionMax
        )

    def getConnection(self):
        """
        Get a connection from the connection pool.
        This will retrieve a connection from the connection pool L{Queue}
        object.
        If the L{Queue} is empty, it will check to see whether a new connection
        can be created (based on the connection limit), and if so create that
        and use it.
        If no new connections can be created, it will block on the L{Queue}
        until an existing, in-use, connection is put back.
        """
        try:
            connection = self.connectionQueue.get(block=False)
        except Empty:
            # Note we use a lock here to prevent a race condition in which
            # multiple requests for a new connection could succeed even though
            # the connection counts starts out one less than the maximum.
            # This can happen because self._connect() can take a while.
            self.connectionCreateLock.acquire()
            if len(self.connections) < self.connectionMax:
                connection = self._connect()
                self.connections.append(connection)
                self.connectionCreateLock.release()
            else:
                self.connectionCreateLock.release()
                self.ds.poolStats["connection-{}-blocked".format(self.poolName)] += 1
                connection = self.connectionQueue.get()

        connectionID = "connection-{}-{}".format(
            self.poolName, self.connections.index(connection)
        )

        self.ds.poolStats[connectionID] += 1
        self.activeCount = len(self.connections) - self.connectionQueue.qsize()
        self.ds.poolStats["connection-{}-active".format(self.poolName)] = self.activeCount
        self.ds.poolStats["connection-{}-max".format(self.poolName)] = max(
            self.ds.poolStats["connection-{}-max".format(self.poolName)], self.activeCount
        )

        if self.activeCount > self.connectionMax:
            self.log.error(
                "[Pool: {pool}] Active LDAP connections ({active}) exceeds maximum ({max})",
                pool=self.poolName, active=self.activeCount, max=self.connectionMax
            )
        return connection

    def returnConnection(self, connection):
        """
        A connection is no longer needed - return it to the pool.
        """
        self.connectionQueue.put(connection)
        self.activeCount = len(self.connections) - self.connectionQueue.qsize()
        self.ds.poolStats["connection-{}-active".format(self.poolName)] = self.activeCount

    def failedConnection(self, connection):
        """
        A connection has failed; remove it from the list of active connections.
        A new one will be created if needed.
        """
        self.ds.poolStats["connection-{}-errors".format(self.poolName)] += 1
        self.connections.remove(connection)
        self.activeCount = len(self.connections) - self.connectionQueue.qsize()
        self.ds.poolStats["connection-{}-active".format(self.poolName)] = self.activeCount

    def _connect(self):
        """
        Connect to the directory server.
        This will always be called in a thread to prevent blocking.

        @returns: The connection object.
        @rtype: L{ldap.ldapobject.LDAPObject}

        @raises: L{LDAPConnectionError} if unable to connect.
        """

        self.log.debug(
            "[Pool: {pool}] Connecting to LDAP at {url}",
            pool=self.poolName, url=self.ds.url
        )
        connection = self._newConnection()

        if self.credentials is not None:
            if IUsernamePassword.providedBy(self.credentials):
                try:
                    connection.simple_bind_s(
                        self.credentials.username,
                        self.credentials.password,
                    )
                    self.log.debug(
                        "[Pool: {pool}] Bound to LDAP as {credentials.username}",
                        pool=self.poolName, credentials=self.credentials
                    )
                except (
                    ldap.INVALID_CREDENTIALS, ldap.INVALID_DN_SYNTAX
                ) as e:
                    self.log.error(
                        "[Pool: {pool}] Unable to bind to LDAP as {credentials.username}",
                        pool=self.poolName, credentials=self.credentials
                    )
                    raise LDAPBindAuthError(
                        self.credentials.username, e
                    )

            else:
                raise LDAPConnectionError(
                    "Unknown credentials type: {0}"
                    .format(self.credentials)
                )

        return connection

    def _newConnection(self):
        """
        Create a new LDAP connection and initialize and start TLS if required.
        This will always be called in a thread to prevent blocking.

        @returns: The connection object.
        @rtype: L{ldap.ldapobject.LDAPObject}

        @raises: L{LDAPConnectionError} if unable to connect.
        """
        connection = ldap.initialize(self.ds.url)

        # FIXME: Use trace_file option to wire up debug logging when
        # Twisted adopts the new logging stuff.

        for option, value in (
            (ldap.OPT_TIMEOUT, self.ds._timeout),
            (ldap.OPT_X_TLS_CACERTFILE, self.ds._tlsCACertificateFile),
            (ldap.OPT_X_TLS_CACERTDIR, self.ds._tlsCACertificateDirectory),
            (ldap.OPT_DEBUG_LEVEL, self.ds._debug),
        ):
            if value is not None:
                connection.set_option(option, value)

        if self.ds._useTLS:
            self.log.debug(
                "[Pool: {pool}] Starting TLS for {url}",
                pool=self.poolName, url=self.ds.url
            )
            connection.start_tls_s()

        self.connectionsCreated += 1

        return connection


#
# Directory Service
#

class DirectoryService(BaseDirectoryService):
    """
    LDAP directory service.
    """

    log = Logger()

    fieldName = ConstantsContainer((BaseFieldName, FieldName))

    recordType = ConstantsContainer((
        BaseRecordType.user, BaseRecordType.group,
    ))

    def __init__(
        self,
        url,
        baseDN,
        credentials=None,
        timeout=None,
        tlsCACertificateFile=None,
        tlsCACertificateDirectory=None,
        useTLS=False,
        fieldNameToAttributesMap=DEFAULT_FIELDNAME_ATTRIBUTE_MAP,
        recordTypeSchemas=DEFAULT_RECORDTYPE_SCHEMAS,
        extraFilters=None,
        ownThreadpool=True,
        threadPoolMax=10,
        authConnectionMax=5,
        queryConnectionMax=5,
        tries=3,
        warningThresholdSeconds=5,
        _debug=False,
    ):
        """
        @param url: The URL of the LDAP server to connect to.
        @type url: L{unicode}

        @param baseDN: The base DN for queries.
        @type baseDN: L{unicode}

        @param credentials: The credentials to use to authenticate with the
            LDAP server.
        @type credentials: L{IUsernamePassword}

        @param timeout: A timeout, in seconds, for LDAP queries.
        @type timeout: number

        @param tlsCACertificateFile: ...
        @type tlsCACertificateFile: L{FilePath}

        @param tlsCACertificateDirectory: ...
        @type tlsCACertificateDirectory: L{FilePath}

        @param useTLS: Enable the use of TLS.
        @type useTLS: L{bool}

        @param fieldNameToAttributesMap: A mapping of field names to LDAP
            attribute names.
        @type fieldNameToAttributesMap: mapping with L{NamedConstant} keys and
            sequence of L{unicode} values

        @param recordTypeSchemas: Schema information for record types.
        @type recordTypeSchemas: mapping from L{NamedConstant} to
            L{RecordTypeSchema}

        @param extraFilters: A dict (keyed off recordType) of extra filter
            fragments to AND in to any generated queries.
        @type extraFilters: L{dicts} of L{unicode}
        """
        self.url = url
        self._baseDN = baseDN
        self._credentials = credentials
        self._timeout = timeout
        self._extraFilters = extraFilters
        self._tries = tries
        self._warningThresholdSeconds = warningThresholdSeconds

        if tlsCACertificateFile is None:
            self._tlsCACertificateFile = None
        else:
            self._tlsCACertificateFile = tlsCACertificateFile.path

        if tlsCACertificateDirectory is None:
            self._tlsCACertificateDirectory = None
        else:
            self._tlsCACertificateDirectory = tlsCACertificateDirectory.path

        self._useTLS = useTLS

        if _debug:
            self._debug = 255
        else:
            self._debug = None

        if self.fieldName.recordType in fieldNameToAttributesMap:
            raise TypeError("Record type field may not be mapped")

        if BaseFieldName.uid not in fieldNameToAttributesMap:
            raise DirectoryConfigurationError("Mapping for uid required")

        self._fieldNameToAttributesMap = fieldNameToAttributesMap

        self._attributeToFieldNameMap = {}
        for name, attributes in fieldNameToAttributesMap.iteritems():
            for attribute in attributes:
                if ":" in attribute:
                    attribute, ignored = attribute.split(":", 1)
                self._attributeToFieldNameMap.setdefault(
                    attribute, []
                ).append(name)

        self._recordTypeSchemas = recordTypeSchemas

        attributesToFetch = set()
        for attributes in fieldNameToAttributesMap.values():
            for attribute in attributes:
                if ":" in attribute:
                    attribute, ignored = attribute.split(":", 1)
                attributesToFetch.add(attribute.encode("utf-8"))
        self._attributesToFetch = list(attributesToFetch)

        # Threaded connection pool.
        # The connection size limit here is the size for connections doing
        # queries.
        # There will also be one-off connections for authentications which also
        # run in their own threads.
        # Thus the threadpool max ought to be larger than the connection max to
        # allow for both pooled query connections and one-off auth-only
        # connections.

        self.ownThreadpool = ownThreadpool
        if self.ownThreadpool:
            self.threadpool = ThreadPool(
                minthreads=1, maxthreads=threadPoolMax,
                name="LDAPDirectoryService",
            )
        else:
            # Use the default threadpool but adjust its size to fit our needs
            self.threadpool = reactor.getThreadPool()
            self.threadpool.adjustPoolsize(
                max(threadPoolMax, self.threadpool.max)
            )

        # Separate pools for LDAP queries and LDAP binds.
        self.connectionPools = {
            "query": ConnectionPool("query", self, credentials, queryConnectionMax),
            "auth": ConnectionPool("auth", self, None, authConnectionMax),
        }
        self.poolStats = collections.defaultdict(int)

        reactor.callWhenRunning(self.start)
        reactor.addSystemEventTrigger("during", "shutdown", self.stop)

    def getPreferredRecordTypesOrder(self):
        # Not doing this in init( ) because we get our recordTypes assigned later

        if not hasattr(self, "_preferredRecordTypesOrder"):
            self._preferredRecordTypesOrder = []
            for recordTypeName in ["user", "location", "resource", "group", "address"]:
                try:
                    recordType = self.recordType.lookupByName(recordTypeName)
                    self._preferredRecordTypesOrder.append(recordType)
                except ValueError:
                    pass

        return self._preferredRecordTypesOrder

    def start(self):
        """
        Start up this service. Initialize the threadpool (if we own it).
        """
        if self.ownThreadpool:
            self.threadpool.start()

    def stop(self):
        """
        Stop the service.
        Stop the threadpool if we own it and do other clean-up.
        """
        if self.ownThreadpool:
            self.threadpool.stop()

        # FIXME: we should probably also close the pool of active connections
        # too.

    @property
    def realmName(self):
        return u"{self.url}".format(self=self)

    class Connection(object):
        """
        ContextManager object for getting a connection from the pool.
        On exit the connection will be put back in the pool if no exception was
        raised.
        Otherwise, the connection will be removed from the active connection
        list, which will allow a new "clean" connection to be created later if
        needed.
        """

        def __init__(self, ds, poolName):
            self.pool = ds.connectionPools[poolName]

        def __enter__(self):
            self.connection = self.pool.getConnection()
            return self.connection

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                self.pool.returnConnection(self.connection)
                return True
            else:
                self.pool.failedConnection(self.connection)
                return False

    def _authenticateUsernamePassword(self, dn, password):
        """
        Open a secondary connection to the LDAP server and try binding to it
        with the given credentials

        @returns: True if the password is correct, False otherwise
        @rtype: deferred C{bool}

        @raises: L{LDAPConnectionError} if unable to connect.
        """
        d = deferToThreadPool(
            reactor, self.threadpool,
            self._authenticateUsernamePassword_inThread, dn, password
        )
        qsize = self.threadpool._queue.qsize()
        if qsize > 0:
            self.log.error("LDAP thread pool overflowing: {qsize}", qsize=qsize)
            self.poolStats["connection-thread-blocked"] += 1
        return d

    def _authenticateUsernamePassword_inThread(self, dn, password, testStats=None):
        """
        Open a secondary connection to the LDAP server and try binding to it
        with the given credentials.
        This method is always called in a thread.

        @returns: True if the password is correct, False otherwise
        @rtype: C{bool}

        @raises: L{LDAPConnectionError} if unable to connect.
        """
        self.log.debug("Authenticating {dn}", dn=dn)

        # Retry if we get ldap.SERVER_DOWN
        for retryNumber in xrange(self._tries):

            # For unit tests, a bit of instrumentation so we can examine
            # retryNumber:
            if testStats is not None:
                testStats["retryNumber"] = retryNumber

            try:

                with DirectoryService.Connection(self, "auth") as connection:
                    try:
                        # During testing, allow an exception to be raised.
                        # Note: I tried to use patch( ) to accomplish this
                        # but that seemed to create a race condition in the
                        # restoration of the patched value and that would cause
                        # unit tests to occasionally fail.
                        if testStats is not None:
                            if "raise" in testStats:
                                raise testStats["raise"]

                        connection.simple_bind_s(dn, password)
                        self.log.debug("Authenticated {dn}", dn=dn)
                        return True
                    except (
                        ldap.INAPPROPRIATE_AUTH,
                        ldap.INVALID_CREDENTIALS,
                        ldap.INVALID_DN_SYNTAX,
                    ):
                        self.log.debug("Unable to authenticate {dn}", dn=dn)
                        return False
                    except ldap.CONSTRAINT_VIOLATION:
                        self.log.info("Account locked {dn}", dn=dn)
                        return False
                    except ldap.SERVER_DOWN as e:
                        # Catch this below for retry
                        raise e
                    except Exception as e:
                        self.log.error("Unexpected error {error} trying to authenticate {dn}", error=str(e), dn=dn)
                        return False
                    else:
                        # Do an unauthenticated bind on this connection at the end in
                        # case the server limits the number of concurrent auths by a given user.
                        connection.simple_bind_s("", "")

            except ldap.SERVER_DOWN as e:
                self.log.error("LDAP server unavailable")
                if retryNumber + 1 == self._tries:
                    # We've hit SERVER_DOWN self._tries times, giving up.
                    raise LDAPQueryError("LDAP server down", e)
                else:
                    self.log.error("LDAP connection failure; retrying...")

    def _recordsFromQueryString(
        self, queryString, recordTypes=None,
        limitResults=None, timeoutSeconds=None
    ):
        d = deferToThreadPool(
            reactor, self.threadpool,
            self._recordsFromQueryString_inThread,
            queryString,
            recordTypes,
            limitResults=limitResults,
            timeoutSeconds=timeoutSeconds
        )
        qsize = self.threadpool._queue.qsize()
        if qsize > 0:
            self.log.error("LDAP thread pool overflowing: {qsize}", qsize=qsize)
            self.poolStats["connection-thread-blocked"] += 1
        return d

    def _addExtraFilter(self, recordType, queryString):
        if self._extraFilters and self._extraFilters.get(recordType, ""):
            queryString = "(&{extra}{query})".format(
                extra=self._extraFilters[recordType], query=queryString
            )
        return queryString

    def _recordsFromQueryString_inThread(
        self, queryString, recordTypes=None,
        limitResults=None, timeoutSeconds=None,
        testStats=None
    ):
        # This method is always called in a thread.

        if recordTypes is None:
            # recordTypes = list(self.recordTypes())

            # Quick hack to optimize the order in which we query by record type:
            recordTypes = self.getPreferredRecordTypesOrder()

        # Retry if we get ldap.SERVER_DOWN
        for retryNumber in xrange(self._tries):

            # For unit tests, a bit of instrumentation so we can examine
            # retryNumber:
            if testStats is not None:
                testStats["retryNumber"] = retryNumber

            records = []

            try:

                with DirectoryService.Connection(self, "query") as connection:

                    for recordType in recordTypes:

                        if limitResults is not None:
                            if limitResults < 1:
                                break

                        try:
                            rdn = self._recordTypeSchemas[recordType].relativeDN
                        except KeyError:
                            # Skip this unknown record type
                            continue

                        rdn = (
                            ldap.dn.str2dn(rdn.lower()) +
                            ldap.dn.str2dn(self._baseDN.lower())
                        )
                        filteredQuery = self._addExtraFilter(
                            recordType, queryString
                        )
                        self.log.debug(
                            "Performing LDAP query: "
                            "{rdn} {query} {recordType}{limit}{timeout}",
                            rdn=rdn,
                            query=filteredQuery,
                            recordType=recordType,
                            limit=(
                                " limit={}".format(limitResults)
                                if limitResults else ""
                            ),
                            timeout=(
                                " timeout={}".format(timeoutSeconds)
                                if timeoutSeconds else ""
                            ),
                        )
                        try:
                            startTime = time.time()

                            s = ldap.async.List(connection)
                            s.startSearch(
                                ldap.dn.dn2str(rdn),
                                ldap.SCOPE_SUBTREE,
                                filteredQuery,
                                attrList=self._attributesToFetch,
                                timeout=(
                                    timeoutSeconds
                                    if timeoutSeconds else -1
                                ),
                                sizelimit=(
                                    limitResults
                                    if limitResults else 0
                                ),
                            )
                            s.processResults()

                        except ldap.SIZELIMIT_EXCEEDED as e:
                            self.log.debug(
                                "LDAP result limit exceeded: {limit}",
                                limit=limitResults,
                            )

                        except ldap.TIMELIMIT_EXCEEDED as e:
                            self.log.warn(
                                "LDAP timeout exceeded: {timeout} seconds",
                                timeout=timeoutSeconds,
                            )

                        except ldap.FILTER_ERROR as e:
                            self.log.error(
                                "Unable to perform query {query!r}: {err}",
                                query=queryString, err=e
                            )
                            raise LDAPQueryError("Unable to perform query", e)

                        except ldap.NO_SUCH_OBJECT as e:
                            # self.log.warn(
                            #     "RDN {rdn} does not exist, skipping", rdn=rdn
                            # )
                            continue

                        except ldap.INVALID_SYNTAX as e:
                            self.log.error(
                                "LDAP invalid syntax {query!r}: {err}",
                                query=queryString, err=e
                            )
                            continue

                        except ldap.SERVER_DOWN as e:
                            # Catch this below for retry
                            raise e

                        except Exception as e:
                            self.log.error(
                                "LDAP error {query!r}: {err}",
                                query=queryString, err=e
                            )
                            raise LDAPQueryError("Unable to perform query", e)

                        reply = [
                            resultItem
                            for _ignore_resultType, resultItem
                            in s.allResults
                        ]

                        totalTime = time.time() - startTime
                        if totalTime > self._warningThresholdSeconds:
                            if filteredQuery and len(filteredQuery) > 500:
                                filteredQuery = "%s..." % (filteredQuery[:500],)
                            self.log.error(
                                "LDAP query exceeded threshold: {totalTime:.2f} seconds for {rdn} {query} (#results={resultCount})",
                                totalTime=totalTime, rdn=rdn,
                                query=filteredQuery, resultCount=len(reply)
                            )

                        newRecords = self._recordsFromReply(
                            reply, recordType=recordType
                        )

                        self.log.debug(
                            "Records from LDAP query "
                            "({rdn} {query} {recordType}): {count}",
                            rdn=rdn,
                            query=queryString,
                            recordType=recordType,
                            count=len(newRecords)
                        )

                        if limitResults is not None:
                            limitResults = limitResults - len(newRecords)

                        records.extend(newRecords)

            except ldap.SERVER_DOWN as e:
                self.log.error("LDAP server unavailable")
                if retryNumber + 1 == self._tries:
                    # We've hit SERVER_DOWN self._tries times, giving up.
                    raise LDAPQueryError("LDAP server down", e)
                else:
                    self.log.error("LDAP connection failure; retrying...")

            else:
                # Only retry if we got ldap.SERVER_DOWN, otherwise break out of
                # loop.
                break

        self.log.debug(
            "LDAP result count ({query}): {count}",
            query=queryString,
            count=len(records)
        )

        return records

    def _recordWithDN(self, dn):
        d = deferToThreadPool(
            reactor, self.threadpool,
            self._recordWithDN_inThread, dn
        )
        qsize = self.threadpool._queue.qsize()
        if qsize > 0:
            self.log.error("LDAP thread pool overflowing: {qsize}", qsize=qsize)
            self.poolStats["connection-thread-blocked"] += 1
        return d

    def _recordWithDN_inThread(self, dn, testStats=None):
        """
        @param dn: The DN of the record to search for
        @type dn: C{str}
        """
        # This method is always called in a thread.

        records = []

        # Retry if we get ldap.SERVER_DOWN
        for retryNumber in xrange(self._tries):

            # For unit tests, a bit of instrumentation:
            if testStats is not None:
                testStats["retryNumber"] = retryNumber

            try:

                with DirectoryService.Connection(self, "query") as connection:

                    self.log.debug("Performing LDAP DN query: {dn}", dn=dn)

                    try:
                        reply = connection.search_s(
                            dn,
                            ldap.SCOPE_SUBTREE,
                            "(objectClass=*)",
                            attrlist=self._attributesToFetch
                        )
                        records = self._recordsFromReply(reply)
                    except ldap.NO_SUCH_OBJECT:
                        records = []
                    except ldap.INVALID_DN_SYNTAX:
                        self.log.warn("Invalid LDAP DN syntax: '{dn}'", dn=dn)
                        records = []

            except ldap.SERVER_DOWN as e:
                self.log.error(
                    "LDAP server unavailable"
                )
                if retryNumber + 1 == self._tries:
                    # We've hit SERVER_DOWN self._tries times, giving up
                    raise LDAPQueryError("LDAP server down", e)
                else:
                    self.log.error("LDAP connection failure; retrying...")

            else:
                # Only retry if we got ldap.SERVER_DOWN, otherwise break out of
                # loop
                break

        if len(records):
            return records[0]
        else:
            return None

    def _recordsFromReply(self, reply, recordType=None):
        records = []

        for dn, recordData in reply:

            # Determine the record type
            if recordType is None:
                recordType = recordTypeForDN(
                    self._baseDN, self._recordTypeSchemas, dn
                )

            if recordType is None:
                recordType = recordTypeForRecordData(
                    self._recordTypeSchemas, recordData
                )

            if recordType is None:
                self.log.debug(
                    "Ignoring LDAP record data; unable to determine record "
                    "type: {recordData!r}",
                    recordData=recordData,
                )
                continue

            # Populate a fields dictionary
            fields = {}

            for fieldName, attributeRules in (
                self._fieldNameToAttributesMap.iteritems()
            ):
                valueType = self.fieldName.valueType(fieldName)

                for attributeRule in attributeRules:
                    attributeName = attributeRule.split(":")[0]
                    if attributeName in recordData:
                        values = recordData[attributeName]

                        if valueType in (unicode, UUID):
                            if not isinstance(values, list):
                                values = [values]

                            if valueType is unicode:
                                newValues = []
                                for v in values:
                                    if isinstance(v, unicode):
                                        # because the ldap unit test produces
                                        # unicode values (?)
                                        newValues.append(v)
                                    else:
                                        newValues.append(unicode(v, "utf-8"))
                            else:
                                try:
                                    newValues = [valueType(v) for v in values]
                                except Exception, e:
                                    self.log.warn(
                                        "Can't parse value {name} {values} "
                                        "({error})",
                                        name=fieldName, values=values,
                                        error=str(e)
                                    )
                                    continue

                            if self.fieldName.isMultiValue(fieldName):
                                if fieldName in fields:
                                    fields[fieldName].extend(newValues)
                                else:
                                    fields[fieldName] = newValues
                            else:
                                # First one in the list wins
                                if fieldName not in fields:
                                    fields[fieldName] = newValues[0]

                        elif valueType is bool:
                            if not isinstance(values, list):
                                values = [values]
                            if ":" in attributeRule:
                                ignored, trueValue = attributeRule.split(":")
                            else:
                                trueValue = "true"

                            for value in values:
                                if value == trueValue:
                                    fields[fieldName] = True
                                    break
                            else:
                                fields[fieldName] = False

                        elif issubclass(valueType, Names):
                            if not isinstance(values, list):
                                values = [values]

                            _ignore_attribute, attributeValue, fieldValue = (
                                attributeRule.split(":")
                            )

                            for value in values:
                                if value == attributeValue:
                                    # convert to a constant
                                    try:
                                        fieldValue = (
                                            valueType.lookupByName(fieldValue)
                                        )
                                        fields[fieldName] = fieldValue
                                    except ValueError:
                                        pass
                                    break

                        else:
                            raise LDAPConfigurationError(
                                "Unknown value type {0} for field {1}".format(
                                    valueType, fieldName
                                )
                            )

            # Skip any results missing the uid, which is a required field
            if self.fieldName.uid not in fields:
                continue

            # Set record type and dn fields
            fields[self.fieldName.recordType] = recordType
            fields[self.fieldName.dn] = dn.decode("utf-8")

            # Make a record object from fields.
            record = DirectoryRecord(self, fields)
            records.append(record)

        # self.log.debug("LDAP results: {records}", records=records)

        return records

    def recordsFromNonCompoundExpression(
        self, expression, recordTypes=None, records=None, limitResults=None,
        timeoutSeconds=None
    ):
        if isinstance(expression, MatchExpression):
            queryString = ldapQueryStringFromMatchExpression(
                expression,
                self._fieldNameToAttributesMap, self._recordTypeSchemas
            )
            return self._recordsFromQueryString(
                queryString, recordTypes=recordTypes,
                limitResults=limitResults, timeoutSeconds=timeoutSeconds
            )

        elif isinstance(expression, ExistsExpression):
            queryString = ldapQueryStringFromExistsExpression(
                expression,
                self._fieldNameToAttributesMap, self._recordTypeSchemas
            )
            return self._recordsFromQueryString(
                queryString, recordTypes=recordTypes,
                limitResults=limitResults, timeoutSeconds=timeoutSeconds
            )

        elif isinstance(expression, BooleanExpression):
            queryString = ldapQueryStringFromBooleanExpression(
                expression,
                self._fieldNameToAttributesMap, self._recordTypeSchemas
            )
            return self._recordsFromQueryString(
                queryString, recordTypes=recordTypes,
                limitResults=limitResults, timeoutSeconds=timeoutSeconds
            )

        return BaseDirectoryService.recordsFromNonCompoundExpression(
            self, expression, records=records, limitResults=limitResults,
            timeoutSeconds=timeoutSeconds
        )

    def recordsFromCompoundExpression(
        self, expression, recordTypes=None, records=None,
        limitResults=None, timeoutSeconds=None
    ):
        if not expression.expressions:
            return succeed(())

        queryString = ldapQueryStringFromCompoundExpression(
            expression,
            self._fieldNameToAttributesMap, self._recordTypeSchemas
        )
        return self._recordsFromQueryString(
            queryString, recordTypes=recordTypes,
            limitResults=limitResults, timeoutSeconds=timeoutSeconds
        )

    def recordsWithRecordType(
        self, recordType, limitResults=None, timeoutSeconds=None
    ):
        queryString = ldapQueryStringFromExistsExpression(
            ExistsExpression(self.fieldName.uid),
            self._fieldNameToAttributesMap, self._recordTypeSchemas
        )
        return self._recordsFromQueryString(
            queryString, recordTypes=[recordType],
            limitResults=limitResults, timeoutSeconds=timeoutSeconds
        )

    # def updateRecords(self, records, create=False):
    #     for record in records:
    #         return fail(NotAllowedError("Record updates not allowed."))
    #     return succeed(None)

    # def removeRecords(self, uids):
    #     for uid in uids:
    #         return fail(NotAllowedError("Record removal not allowed."))
    #     return succeed(None)


def splitIntoBatches(data, size):
    """
    Return a generator of lists consisting of the contents of the data set
    split into parts no larger than size.
    """
    if not data:
        yield []
    data = list(data)
    while data:
        yield data[:size]
        del data[:size]


@implementer(IPlaintextPasswordVerifier)
class DirectoryRecord(BaseDirectoryRecord):
    """
    LDAP directory record.
    """

    @inlineCallbacks
    def members(self):

        members = set()

        if self.recordType != self.service.recordType.group:
            returnValue(())

        # Scan through the memberDNs, grouping them by record type (which we
        # deduce by their RDN).  If we have a fieldname that corresponds to
        # the most specific slice of the DN, we can bundle that into a
        # single CompoundExpression to fault in all the DNs belonging to the
        # same base RDN, reducing the number of requests from 1-per-member to
        # 1-per-record-type.  Any memberDNs we can't group in this way are
        # simply faulted in by DN at the end.

        fieldValuesByRecordType = {}
        # dictionary key = recordType, value = tuple(fieldName, value)

        faultByDN = []
        # the DNs we need to fault in individually

        for dnStr in getattr(self, "memberDNs", []):
            try:
                recordType = recordTypeForDN(
                    self.service._baseDN, self.service._recordTypeSchemas, dnStr
                )
                dn = ldap.dn.str2dn(dnStr.lower())
                attrName, value, ignored = dn[0][0]
                fieldName = self.service._attributeToFieldNameMap[attrName][0]
                fieldValues = fieldValuesByRecordType.setdefault(recordType, [])
                fieldValues.append((fieldName, value))
                continue

            except:
                # For whatever reason we can't group this DN in with the others
                # so we'll add it to faultByDN just below
                pass

            # have to fault in by dn
            faultByDN.append(dnStr)

        for recordType, fieldValue in fieldValuesByRecordType.iteritems():
            if fieldValue:
                matchExpressions = []
                for fieldName, value in fieldValue:
                    matchExpressions.append(
                        MatchExpression(
                            fieldName,
                            value.decode("utf-8"),
                            matchType=MatchType.equals
                        )
                    )

                # split into batches of 500
                for batch in splitIntoBatches(matchExpressions, 500):
                    expression = CompoundExpression(
                        batch,
                        Operand.OR
                    )
                    for record in (yield self.service.recordsFromCompoundExpression(
                        expression, recordTypes=[recordType]
                    )):
                        members.add(record)

        for dnStr in faultByDN:
            record = yield self.service._recordWithDN(dnStr.replace("+", "\+"))
            members.add(record)

        returnValue(members)

    # @inlineCallbacks
    def groups(self):
        raise NotImplementedError()

    #
    # Verifiers for twext.who.checker stuff.
    #

    def verifyPlaintextPassword(self, password):
        return self.service._authenticateUsernamePassword(self.dn, password)


def normalizeDNstr(dnStr):
    """
    Convert to lowercase and remove extra whitespace
    @param dnStr: dn
    @type dnStr: C{str}
    @return: normalized dn C{str}
    """
    return ' '.join(ldap.dn.dn2str(ldap.dn.str2dn(dnStr.lower())).split())


def reverseDict(source):
    """
    Reverse keys and values in a mapping.
    """
    new = {}

    for key, values in source.iteritems():
        for value in values:
            new.setdefault(value, []).append(key)

    return new


def recordTypeForDN(baseDnStr, recordTypeSchemas, dnStr):
    """
    Examine a DN to determine which recordType it belongs to

    @param baseDnStr: The base DN
    @type baseDnStr: string

    @param recordTypeSchemas: Schema information for record types.
    @type recordTypeSchemas: mapping from L{NamedConstant} to
        L{RecordTypeSchema}

    @param dnStr: DN to compare
    @type dnStr: string

    @return: recordType string, or None if no match
    """
    dn = ldap.dn.str2dn(dnStr.lower())
    baseDN = ldap.dn.str2dn(baseDnStr.lower())

    for recordType, schema in recordTypeSchemas.iteritems():
        combined = ldap.dn.str2dn(schema.relativeDN.lower()) + baseDN
        if dnContainedIn(dn, combined):
            return recordType
    return None


def dnContainedIn(child, parent):
    """
    Return True if child dn is contained within parent dn, otherwise False.
    """
    return child[-len(parent):] == parent


def recordTypeForRecordData(recordTypeSchemas, recordData):
    """
    Given info about record types, determine the record type for a blob of
    LDAP record data.

    @param recordTypeSchemas: Schema information for record types.
    @type recordTypeSchemas: mapping from L{NamedConstant} to
        L{RecordTypeSchema}

    @param recordData: LDAP record data.
    @type recordData: mapping
    """
    for recordType, schema in recordTypeSchemas.iteritems():
        for attribute, value in schema.attributes:
            dataValue = recordData.get(attribute)
            # If the data value (e.g. objectClass) is a list, see if the
            # expected value is contained in that list, otherwise directly
            # compare.
            if isinstance(dataValue, list):
                if value not in dataValue:
                    break
            else:
                if value != dataValue:
                    break
        else:
            return recordType

    return None
