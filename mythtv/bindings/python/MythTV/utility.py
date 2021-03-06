# -*- coding: utf-8 -*-
"""Provides decorator classes for assorted functions"""

from MythTV.logging import MythLog
from MythTV.exceptions import MythDBError, MythError

from cStringIO import StringIO
from select import select
from time import time, mktime, sleep
from datetime import datetime as _pydatetime
from datetime import tzinfo as _pytzinfo
from datetime import timedelta
from itertools import imap
from threading import Thread, Lock
from collections import deque
from Queue import Queue
import weakref
import socket
import re

def _donothing(*args, **kwargs):
    pass

class SchemaUpdate( object ):
    # TODO: do locking and lock checking
    #       if interactive terminal, ask for update permission
    #       perform database backup (partial?)
    """
    Iteratively calls methods named 'up<schema version>' to update
        schema. Methods must return an integer of the new schema
        version.
    """
    _schema_name = None
    def __init__(self, db):
        self.db = db
        self.log = MythLog('Schema Update (%s)' % self._schema_name)

    def run(self):
        if self._schema_name is None:
            raise MythDBError('Schema update failed, variable name not set')
        if self.db.settings.NULL[self._schema_name] is None:
            self.db.settings.NULL[self._schema_name] = self.create()

        origschema = int(self.db.settings.NULL[self._schema_name])
        schema = origschema
        try:
            while True:
                
                newschema = getattr(self, 'up%d' % schema)()
                self.log(MythLog.GENERAL, MythLog.INFO,
                         'successfully updated from %d to %d' %\
                                (schema, newschema))
                schema = newschema
                self.db.settings.NULL[self._schema_name] = schema

        except AttributeError, e:
            self.log(MythLog.GENERAL, MythLog.CRIT,
                     'failed at %d' % schema, 'no handler method')
            raise MythDBError('Schema update failed, ' 
                    "SchemaUpdate has no function 'up%s'" % schema)

        except StopIteration:
            if schema != origschema:
                self.log(MythLog.GENERAL, MythLog.NOTICE,
                         '%s update complete' % self._schema_name)
            pass

        except Exception, e:
            raise MythDBError(MythError.DB_SCHEMAUPDATE, e.args)

    def create(self):
        raise MythDBError('Schema creation failed, method not defined.')

class databaseSearch( object ):
    # decorator class for database searches
    """
    Decorator class

    Decorated functions must accept three keywords: init, key, value.
        'init' will be set once to True, to initialize the decorator. During
                initialization, the decorated class must return a tuple
                of the following format
                    (<table name>,   -- Primary table to pull data from.
                     <data class>,   -- Data handling class to use to process
                                        data. Ideally a subclass of DBData, 
                                        this class must provide a 'fromRaw'
                                        classmethod.
                     <required keywords>, -- Tuple of keywords that must be
                                             sent to the decorated function.
                                             If not provided by the user when
                                             called, these will be sent with
                                             a value of 'None'.
                     [join definition, [join definition, [...]]]
                            -- Optional definitions for additional tables
                               that can be JOINed in for extra search
                               capacity. Uses the following format:
                        (<table name>,  -- New table to be joined in.
                         <table name>,  -- Existing table to be joined to.
                         <joined fields>,  -- Tuple of fields to be used to
                                              match the two tables in the
                                              join.
                         [joined fields]   -- Optional field names to be used
                                              on the existing table.
                        )
                    )

        When the function is called, the decorator will pass the given
            keywords and values to the function one at a time. The function
            can return one of three responses.
                None     -- Invalid keyword, decorator throws an error.
                3-field  -- Normal response consisting of:
                    (<where statement>, -- like 'title=%s'
                     <where value>,
                     <bitwise>          -- defines which if any tables need
                                           to be JOINed in
                    )
                4-field  -- Special response consisting of:
                    (see example in methodheap.py:MythDB.searchRecorded)
    """
    class Join( object ):
        def __init__(self, table=None, tableto=None, fields=None, \
                           fieldsto=None, fieldsfrom=None):
            if (table is None) or (tableto is None) or \
                    ((fields is None) and \
                        ((fieldsto is None) or (fieldsfrom is None))):
                raise MythDBError('Invalid input to databaseSearch.Join.')
            self.table = table
            self.tableto = tableto
            if fields:
                self.fieldsfrom = fields
                self.fieldsto = fields
            else:
                self.fieldsfrom = fieldsfrom
                self.fieldsto = fieldsto

        def buildWhere(self):
            s = '%s.%%s=%s.%%s' % (self.table, self.tableto)
            return ' AND '.join([s % (f,t) \
                        for f,t in zip(self.fieldsfrom,self.fieldsto)])

        def buildJoin(self):
            return 'JOIN %s ON %s' % (self.table, self.buildWhere())

    def __init__(self, func):
        # set function and update strings
        self.func = func
        self.__doc__ = self.func.__doc__
        self.__name__ = self.func.__name__
        self.__module__ = self.func.__module__

        # set defaults
        self.table = None
        self.handler = None
        self.require = ()
        self.joins = ()

        # pull in properties
        self.func(self, self)

        # sanity check
        if (self.table is None) or (self.handler is None):
            raise MythError('Improperly configured databaseSearch class')

    def __get__(self, inst, own):
        # set instance and return self
        self.inst = inst
        return self

    def __call__(self, **kwargs):
        where,fields,joinbit = self.parseInp(kwargs)

        # process query
        query = self.buildQuery(where, joinbit=joinbit)
        with self.inst.cursor(self.inst.log) as cursor:
            if len(where) > 0:
                cursor.execute(query, fields)
            else:
                cursor.execute(query)

            for row in cursor:
                yield self.handler.fromRaw(row, db=self.inst)

    def parseInp(self, kwargs):
        where = []
        fields = []
        joinbit = 0

        # loop through inputs
        for key, val in kwargs.items():
            if val is None:
                continue

            # process custom query
            if key == 'custom':
                custwhere = {}
                custwhere.update(val)
                for k,v in custwhere.items():
                    where.append(k)
                    fields.append(v)
                continue

            # let function process remaining queries
            res = self.func(self.inst, key=key, value=val)
            errstr = "%s got an unexpected keyword argument '%s'"
            if res is None:
                if 'not' not in key:
                    raise TypeError(errstr % (self.__name__, key))
                # try inverted argument
                res = list(self.func(self.inst, key=key[3:], value=val))
                if res is None:
                    raise TypeError(errstr % (self.__name__, key))
                res[0] = 'NOT '+res[0]

            if len(res) == 0:
                continue
            elif len(res) == 3:
                # normal processing
                where.append(res[0])
                fields.append(res[1])
                joinbit = joinbit|res[2]
            elif len(res) == 4:
                # special format for crossreferenced data
                lval = [f.strip() for f in val.split(',')]
                where.append('(%s)=%d' %\
                    (self.buildQuery(
                        (   self.joins[res[3]].buildWhere(),
                            '(%s)' % \
                                 ' OR '.join(['%s=?' % res[0] \
                                                    for f in lval])),
                        'COUNT( DISTINCT %s )' % res[0],
                        res[1],
                        res[2]),
                    len(lval)))
                fields += lval
                            
        for key in self.require:
            if key not in kwargs:
                res = self.func(self.inst, key=key)
                if res is None:
                    continue
                where.append(res[0])
                fields.append(res[1])
                joinbit = joinbit|res[2]

        return where,fields,joinbit

    def buildJoinOn(self, i):
        if len(self.joins[i]) == 3:
            # shared field name
            on = ' AND '.join(['%s.%s=%s.%s' % \
                    (self.joins[i][0], field,\
                     self.joins[i][1], field)\
                             for field in self.joins[i][2]])
        else:
            # separate field name
            on = ' AND '.join(['%s.%s=%s.%s' % \
                    (self.joins[i][0], ffield,\
                     self.joins[i][1], tfield)\
                             for ffield, tfield in zip(\
                                    self.joins[i][2],\
                                    self.joins[i][3])])
        return on

    def buildJoin(self, joinbit=0):
        join = []
        for i,v in enumerate(self.joins):
            if (2**i)&joinbit:
                join.append(v.buildJoin())
        return ' '.join(join)

    def buildQuery(self, where, select=None, tfrom=None, joinbit=0):
        sql = 'SELECT '
        if select:
            sql += select
        elif tfrom:
            sql += tfrom+'.*'
        else:
            sql += self.table+'.*'

        sql += ' FROM '
        if tfrom:
            sql += tfrom
        else:
            sql += self.table

        sql += ' '+self.buildJoin(joinbit)

        if len(where):
            sql += ' WHERE '
            sql += ' AND '.join(where)

        return sql

class CMPVideo( object ):
    """
    Utility class providing comparison operators between data objects
        containing video metadata.  
    """
    _lt__ = None
    _gt__ = None
    _eq__ = None

    def __le__(self, other): return (self<other) or (self==other)
    def __ge__(self, other): return (self>other) or (self==other)
    def __ne__(self, other): return not self==other

    def __lt__(self, other):
        """
        If season and episode are defined for both and inetref matches, use
            those. Otherwise, use title and subtitle
        """
        try:
            # assume table has already been populated for other type, and
            # perform comparison using stored lambda function
            return self._lt__[type(other)](self, other)
        except TypeError:
            # class has not yet been configured yet
            # create dictionary for storage of type comparisons
            self.__class__._lt__ = {}
        except KeyError:
            # key error means no stored comparison function for other type
            # pass through so it can be built
            # all other errors such as NotImplemented should continue upstream
            pass

        attrs = ('inetref', 'season', 'episode')
        null = ('00000000', '0', '')

        check = None
        if all(hasattr(self, a) for a in attrs) and \
                all(hasattr(other, a) for a in attrs):
            # do a match using all of inetref, season, and episode
            # use title/subtitle instead if inetref is undefined
            check = lambda s,o: (s.title, s.subtitle) < \
                                (o.title, o.subtitle) \
                                if \
                                    ((s.inetref in null) or \
                                     (o.inetref in null)) \
                                else \
                                    False \
                                    if \
                                        s.inetref != o.inetref \
                                    else \
                                        [s[a] for a in attrs] < \
                                        [o[a] for a in attrs]
        elif hasattr(self, 'inetref') and hasattr(other, 'inetref'):
            # match using inetref only
            check = lambda s,o: s.inetref < o.inetref \
                                if \
                                    ((s.inetref not in null) and \
                                     (o.inetref not in null)) \
                                else \
                                    (s.title, s.subtitle) < \
                                    (o.title, o.subtitle)
        elif hasattr(self, 'title') and hasattr(self, 'subtitle') and \
                hasattr(other, 'title') and hasattr(other, 'subtitle'):
            # fall through to title and subtitle
            check = lambda s,o: (s.title, s.subtitle) < (o.title, o.subtitle)
        else:
            # no matching options, use ids
            check = lambda s,o: id(s) < id(o)

        # store comparison and return result
        self._lt__[type(other)] = check
        return check(self, other)

    def __eq__(self, other):
        """
        If inetref is defined, following through to season and episode, use
            those.  Otherwise, fall back to title and subtitle.
        """
        try:
            return self._eq__[type(other)](self, other)
        except TypeError:
            self.__class__._eq__ = {}
        except KeyError:
            pass

        attrs = ('inetref', 'season', 'episode')
        null = ('00000000', '0', '')

        check = None
        if all(hasattr(self, a) for a in attrs) and \
                all(hasattr(other, a) for a in attrs):
            check = lambda s,o: [s[a] for a in attrs] == \
                                [o[a] for a in attrs] \
                                if \
                                    ((s.inetref not in null) and \
                                     (o.inetref not in null)) \
                                else \
                                    (s.title, s.subtitle) == \
                                    (o.title, o.subtitle)
        elif hasattr(self, 'inetref') and hasattr(other, 'inetref'):
            check = lambda s,o: s.inetref == o.inetref \
                                if \
                                    ((s.inetref not in null) and \
                                     (o.inetref not in null)) \
                                else \
                                    (s.title, s.subtitle) == \
                                    (o.title, o.subtitle)
        elif hasattr(self, 'title') and hasattr(self, 'subtitle') and \
                hasattr(other, 'title') and hasattr(other, 'subtitle'):
            # fall through to title and subtitle
            check = lambda s,o: (s.title, s.subtitle) == (o.title, o.subtitle)
        else:
            # no matching options, use ids
            check = lambda s,o: id(s) == id(o)

        # store comparison and return result
        self._eq__[type(other)] = check
        return check(self, other)

    def __gt__(self, other):
        """
        If season and episode are defined for both and inetref matches, use
            those. Otherwise, use title and subtitle
        """
        try:
            return self._gt__[type(other)](self, other)
        except TypeError:
            self.__class__._gt__ = {}
        except KeyError:
            pass

        attrs = ('inetref', 'season', 'episode')
        null = ('00000000', '0', '')

        check = None
        if all(hasattr(self, a) for a in attrs) and \
                all(hasattr(other, a) for a in attrs):
            check = lambda s,o: (s.title, s.subtitle) > \
                                (o.title, o.subtitle) \
                                if \
                                    ((s.inetref in null) or \
                                     (o.inetref in null)) \
                                else \
                                    False \
                                    if \
                                        s.inetref != o.inetref \
                                    else \
                                        [s[a] for a in attrs] > \
                                        [o[a] for a in attrs]
        elif hasattr(self, 'inetref') and hasattr(other, 'inetref'):
            check = lambda s,o: s.inetref > o.inetref \
                                if \
                                    ((s.inetref not in null) and \
                                     (o.inetref not in null)) \
                                else \
                                    (s.title, s.subtitle) > \
                                    (o.title, o.subtitle)
        elif hasattr(self, 'title') and hasattr(self, 'subtitle') and \
                hasattr(other, 'title') and hasattr(other, 'subtitle'):
            # fall through to title and subtitle
            check = lambda s,o: (s.title, s.subtitle) > (o.title, o.subtitle)
        else:
            # no matching options, use ids
            check = lambda s,o: id(s) > id(o)

        # store comparison and return result
        self._gt__[type(other)] = check
        return check(self, other)

class CMPRecord( CMPVideo ):
    """
    Utility class providing comparison operators between data objects
        containing video metadata.
    """
    @staticmethod
    def __choose_comparison(self, other):
        l = None
        r = None
        if (hasattr(self, 'recstartts') or hasattr(self, 'progstart')) and \
                (hasattr(other, 'recstartts') or hasattr(other, 'progstart')):
            if hasattr(self, 'recstartts'):
                l = 'recstartts'
            else:
                l = 'starttime'
            if hasattr(other, 'recstartts'):
                r = 'recstartts'
            else:
                r = 'starttime'
        # try to run comparison on program start second
        elif (hasattr(self, 'progstart') or hasattr(self, 'starttime')) and \
                (hasattr(other, 'progstart') or hasattr(other, 'starttime')):
            if hasattr(self, 'progstart'):
                l = 'progstart'
            else:
                l = 'starttime'
            if hasattr(other, 'progstart'):
                r = 'progstart'
            else:
                r = 'starttime'
        return l,r

    def __lt__(self, other):
        try:
            # assume table has already been populated for other type, and
            # perform comparison using stored lambda function
            return self._lt__[type(other)](self, other)
        except TypeError:
            # class has not yet been configured yet
            # create dictionary for storage of type comparisons
            self.__class__._lt__ = {}
        except KeyError:
            # key error means no stored comparison function for other type
            # pass through so it can be built
            # all other errors such as NotImplemented should continue upstream
            pass

        if not (hasattr(self, 'chanid') or hasattr(other, 'chanid')):
            # either object is not a recording-related object
            # fall through to video matching
            return super(CMPRecord, self).__lt__(other)

        # select attributes to use for comparison
        l,r = self.__choose_comparison(self, other)
        if None in (l,r):
            return super(CMPRecord, self).__lt__(other)

        # generate comparison function, and return result
        check = lambda s,o: (s.chanid, s[l]) < (o.chanid, o[l])
        self._lt__[type(other)] = check
        return check(self, other)

    def __eq__(self, other):
        try:
            return self._eq__[type(other)](self, other)
        except TypeError:
            self.__class__._eq__ = {}
        except KeyError:
            pass

        if not (hasattr(self, 'chanid') or hasattr(other, 'chanid')):
            return super(CMPRecord, self).__eq__(other)

        l,r = self.__choose_comparison(self, other)
        if None in (l,r):
            return super(CMPRecord, self).__eq__(other)

        check = lambda s,o: (s.chanid, s[l]) == (o.chanid, o[l])
        self._eq__[type(other)] = check
        return check(self, other)

    def __gt__(self, other):
        try:
            return self._gt__[type(other)](self, other)
        except TypeError:
            self.__class__._gt__ = {}
        except KeyError:
            pass

        if not (hasattr(self, 'chanid') or hasattr(other, 'chanid')):
            return super(CMPRecord, self).__gt__(other)

        l,r = self.__choose_comparison(self, other)
        if None in (l,r):
            return super(CMPRecord, self).__gt__(other)

        check = lambda s,o: (s.chanid, s[l]) > (o.chanid, o[l])
        self._gt__[type(other)] = check
        return check(self, other)

class deadlinesocket( socket.socket ):
    """
    Customized socket providing logging, and several convenience functions for
        connecting with frontend and backend, guaranteeing termination after
        a set timeout.
    """
    def __init__(self, *args, **kwargs):
        socket.socket.__init__(self, *args, **kwargs)
        self.log = MythLog('Python Socket')
        self.setdeadline(10.0)

    def connect(self, *args, **kwargs):
        self.settimeout(self.getdeadline())
        socket.socket.connect(self, *args, **kwargs)
        self.setblocking(0)

    def getdeadline(self): return self._deadline
    def setdeadline(self, deadline): self._deadline = deadline

    def dlrecv(self, bufsize, flags=0, deadline=None):
        # pull default timeout
        if deadline is None:
            deadline = self._deadline
        if deadline < 1000:
            deadline += time()

        buff = StringIO()
        # loop until necessary data has been received
        while bufsize > buff.tell():
            # wait for data on the socket
            t = time()
            timeout = (deadline-t) if (deadline-t>0) else 0.0
            if len(select([self],[],[], timeout)[0]) == 0:
                # deadline reached, terminate
                return u''

            # append response to buffer
            p = buff.tell()
            try:
                buff.write(self.recv(bufsize-buff.tell(), flags))
            except socket.error, e:
                raise MythError(MythError.SOCKET, e.args)
            if buff.tell() == p:
               # no data read from a 'ready' socket, connection terminated
                raise MythError(MythError.SOCKET, (54, 'Connection reset by peer'))

            if timeout == 0:
                break
        return buff.getvalue()

    def dlexpect(self, pattern, flags=0, deadline=None):
        """Loop recv listening for a provided regular expression."""
        # pull default timeout
        if deadline is None:
            deadline = self._deadline
        if deadline < 1000:
            deadline += time()

        buff = StringIO()
        # loop until pattern has been found
        while not pattern.search(buff.getvalue()):
            # wait for data on the socket
            t = time()
            timeout = (deadline-t) if (deadline-t>0) else 0.0
            if len(select([self],[],[], timeout)[0]) == 0:
                # deadline reached, terminate
                return ''

            # append response to buffer
            p = buff.tell()
            try:
                buff.write(self.recv(100, flags))
            except socket.error, e:
                raise MythError(MythError.SOCKET, e.args)
            if buff.tell() == p:
                # no data read from a 'ready' socket, connection terminated
                raise MythError(MythError.CLOSEDSOCKET)

            if timeout == 0:
                break
        return buff.getvalue()

    def recvheader(self, flags=0, deadline=None):
        """
        Loop recv listening for an amount of data provided
            in the first 8 bytes.
        """
        size = int(self.dlrecv(8, flags, deadline))
        data = self.dlrecv(size, flags, deadline)
        self.log(MythLog.SOCKET|MythLog.NETWORK, MythLog.DEBUG, \
                            'read <-- %d' % size, data)
        return data

    def sendheader(self, data, flags=0):
        """Send data, prepending the length in the first 8 bytes."""
        try:
            self.log(MythLog.SOCKET|MythLog.NETWORK, MythLog.DEBUG, \
                                'write --> %d' % len(data), data)
            data = '%-8d%s' % (len(data), data)
            self.send(data, flags)
        except socket.error, e:
            raise MythError(MythError.SOCKET, e.args)

class MARKUPLIST( object ):
    """
    Utility class for building seek/cutlists from video markup data.
    """
    def _buildlist(self, ms, me):
        start = []
        stop = []
        for mark in sorted(self, key=lambda m: m.mark):
            if mark.type == ms:
                if len(start) == len(stop):
                    # start new cut
                    start.append(mark.mark)
                #else: a cut has already been started
            elif mark.type == me:
                if len(start) == 0:
                    # endpoint found without startpoint
                    start.append(0)
                if len(start) > len(stop):
                    # end cut
                    stop.append(mark.mark)
                else:
                    # replace cut endpoint
                    stop[len(stop)-1] = mark.mark
        if len(start) > len(stop):
            # endpoint missing, no known better option
            stop.append(9999999)
        return zip(start, stop)

def levenshtein(s1, s2):
    """Compute the Levenshtein distance of two strings."""
    # http://en.wikibooks.org/wiki/Algorithm_implementation/Strings/Levenshtein_distance
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if not s1:
        return len(s2)
 
    previous_row = xrange(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
 
    return previous_row[-1]

class datetime( _pydatetime ):
    _reiso = re.compile('(?P<year>[0-9]{4})'
                       '-(?P<month>[0-9]{1,2})'
                       '-(?P<day>[0-9]{1,2})'
                        '.'
                        '(?P<hour>[0-9]{2})'
                       ':(?P<min>[0-9]{2})'
                       '(:(?P<sec>[0-9]{2}))?'
                        '(?P<tz>Z|'
                            '(?P<tzdirec>[-+])'
                            '(?P<tzhour>[0-9]{1,2})'
                            '(:)?'
                            '(?P<tzmin>[0-9]{2})?'
                        ')?')

    class _tzinfo( _pytzinfo):
        def __init__(self, direc='+', hr=0, min=0):
            if direc == '-':
                hr = -1*int(hr)
            self._offset = timedelta(hours=int(hr), minutes=int(min))
        def utcoffset(self, dt): return self._offset
        def tzname(self, dt): return ''
        def dst(self, dt): return timedelta(0)

    @classmethod
    def fromTimestamp(cls, posix):
        return cls.fromtimestamp(float(posix))

    @classmethod
    def frommythtime(cls, mtime):
        return cls.strptime(str(mtime), '%Y%m%d%H%M%S')

    @classmethod
    def fromIso(cls, isotime, sep='T'):
        match = cls._reiso.match(isotime)
        if match is None:
            raise TypeError("time data '%s' does not match ISO 8601 format" \
                                % isotime)

        dt = [int(a) for a in match.groups()[:5]]
        if match.group('sec') is not None:
            dt.append(int(match.group('sec')))
        else:
            dt.append(0)
        if match.group('tz'):
            if match.group('tz') == 'Z':
                tz = cls._tzinfo()
            elif match.group('tzmin'):
                tz = cls._tzinfo(*match.group('tzdirec','tzhour','tzmin'))
            else:
                tz = cls._tzinfo(*match.group('tzdirec','tzhour'))
            dt.append(0)
            dt.append(tz)
        return cls(*dt)

    @classmethod
    def fromRfc(cls, rfctime):
        return cls.strptime(rfctime.strip(), '%a, %d %b %Y %H:%M:%S %Z')

    @classmethod
    def duck(cls, t):
        try:
            # existing modified datetime
            t.mythformat
            return t
        except: pass
        try:
            # existing built-in datetime
            return cls.fromIso(t.isoformat())
        except: pass
        for func in [cls.fromTimestamp, #epoch time
                     cls.frommythtime, #iso time with integer characters only
                     cls.fromIso, #iso 8601 time
                     cls.fromRfc]: #rfc 822 time
            try:
                return func(t)
            except:
                pass
        raise TypeError("time data '%s' does not match supported formats"%t)

    def mythformat(self):
        return self.strftime('%Y%m%d%H%M%S')

    def timestamp(self):
        return int(mktime(self.timetuple()))

    def rfcformat(self):
        return self.strftime('%a, %d %b %Y %H:%M:%S %Z')

class ParseEnum( object ):
    _static = None
    def __str__(self): 
        return str([k for k,v in self.iteritems() if v==True])
    def __repr__(self): return str(self)
    def __init__(self, parent, field_name, enum, editable=True):
        self._parent = weakref.proxy(parent)
        self._field = field_name
        self._enum = enum
        self._static = not editable

    def __getattr__(self, name):
        if name in self.__dict__:
            return self.__dict__[name]
        return bool(self._parent[self._field]&getattr(self._enum, name))

    def __setattr__(self, name, value):
        if self._static is None:
            object.__setattr__(self, name, value)
            return
        if self._static:
            raise AttributeError("'%s' cannot be edited." % name)
        self.__setitem__(name, value)

    def __getitem__(self, key):
        return bool(self._parent[self._field]&getattr(self._enum, key))

    def __setitem__(self, key, value):
        if self._static:
            raise KeyError("'%s' cannot be edited." % name)
        val = getattr(self._enum, key)
        if value:
            self._parent[self._field] |= val
        else:
            self._parent[self._field] -= self._parent[self._field]&val

    def keys(self):
        return [key for key in self._enum.__dict__ if key[0] != '_']

    def values(self):
        return list(self.itervalues())

    def items(self):
        return list(self.iteritems())

    def __iter__(self):
        return self.itervalues()

    def iterkeys(self):
        return iter(self.keys())

    def itervalues(self):
        return imap(self.__getitem__, self.keys())

    def iteritems(self):
        for key in self.keys():
            yield (key, self[key])

class ParseSet( ParseEnum ):
    def __init__(self, parent, field_name, editable=True):
        self._parent = weakref.proxy(parent)
        self._field = field_name
        field = parent._db.tablefields[parent._table][self._field].type
        if field[:4] != 'set(':
            raise MythDBError("ParseSet error. "+\
                        "Field '%s' not of type 'set()'" % self._field)
        self._enum = dict([(t,2**i) for i,t in enumerate([type.strip("'")\
                                for type in field[4:-1].split(',')])])
        self._static = not editable

    def __getattr__(self, name):
        if name in self.__dict__:
            return self.__dict__[name]
        return self.__getitem__(name)

    def __setattr__(self, name, value):
        if self._static is None:
            object.__setattr__(self, name, value)
            return
        if self._static:
            raise AttributeError("'%s' cannot be edited." % name)
        self.__setitem__(name, value)

    def __getitem__(self, key):
        return key in self._parent[self._field].split(',')

    def __setitem__(self, key, value):
        if self._static:
            raise KeyError("'%s' cannot be edited." % name)
        if self[key] == value:
            return
        tmp = self._parent[self._field].split(',')
        if value:
            tmp.append(key)
        else:
            tmp.remove(key)
        self._parent[self._field] = ','.join(tmp)

    def keys(self):
        return self._enum.keys()

def CopyData(dfrom, dto, keys, lower=False):
    def copystraight(dfrom, dto, key): dto[key] = dfrom[key]
    def copylower(dfrom, dto, key): dto[key.lower()] = dfrom[key]

    copydata = copystraight
    if lower: copydata = copylower

    for key in keys:
        if key in dfrom:
            copydata(dfrom, dto, key)

def CopyData2(dfrom, dto, keys):
    if hasattr(keys, 'items'):
        keys = keys.items()

    for key,key2 in keys:
        if key in dfrom:
            dto[key2] = dfrom[key]

def check_ipv6(n):
    try:
        socket.inet_pton(socket.AF_INET6, n)
        return True
    except socket.error:
        return False

try:
    from select import poll, POLLHUP, POLLIN, POLLOUT
    class _PollingThread( Thread ):
        """
        This polling thread listens on selected pipes, and automatically reads
        and writes data between the buffer and those pipes. This will self
        terminate when there are no more pipes defined, and will need to be
        restarted.
        """
        def __init__(self, group=None, target=None, name=None,
                           args=(), kwargs={}):
            self.inputqueue = Queue()
            self.idletime = time()
            super(_PollingThread, self).__init__(group,
                        target, name, args, kwargs)
        def add_pipe(self, buff, pipe, mode):
            self.inputqueue.put((buff, pipe, mode))
        def run(self):
            poller = poll()
            fds = {}
            events = []
            while True:
                while not self.inputqueue.empty():
                    # loop though the queue and add new pipes to the
                    # poll object
                    buff, pipe, mode = self.inputqueue.get()
                    if 'r' in mode:
                        poller.register(pipe.fileno(), POLLIN|POLLHUP)
                    elif 'w' in mode:
                        poller.register(pipe.fileno(), POLLOUT|POLLHUP)
                    else:
                        continue
                    fds[pipe.fileno()] = (weakref.ref(buff), pipe)

                for fd,event in poller.poll(0.1):
                    # loop through file numbers and handle events
                    buff, pipe = fds[fd]
                    if buff() is None:
                        # buffer object has closed out from underneath us
                        # remove reference from poller
                        pipe.close()
                        del fds[fd]
                        poller.unregister(fd)
                        continue

                    if event & POLLIN:
                        # read as much data from the pipe as it has available
                        buff().write(pipe.read(2**16))
                    if event & POLLOUT:
                        # write as much data to the pipe as there is space for
                        # roll back buffer if data is not fully written
                        data = buff().read(2**16)
                        nbytes = pipe.write(data)
                        if nbytes != len(data):
                            buff()._rollback(len(data) - nbytes)
                    if event & POLLHUP:
                        # pipe has closed, and all reads have been processed
                        # remove reference from poller
                        buff().close()
                        pipe.close()
                        del fds[fd]
                        poller.unregister(fd)

                if len(fds) == 0:
                    # no pipes referenced
                    if self.idletime + 20 < time():
                        # idle timeout reached, terminate
                        break
                    sleep(0.1)
                else:
                    self.idletime = time()
except ImportError:
    from select import kqueue, kevent, KQ_FILTER_READ, KQ_FILTER_WRITE, \
                         KQ_EV_ADD, KQ_EV_DELETE, KQ_EV_EOF
    class _PollingThread( Thread ):
        """
        This polling thread listens on selected pipes, and automatically reads
        and writes data between the buffer and those pipes. This will self
        terminate when there are no more pipes defined, and will need to be
        restarted.
        """
        def __init__(self, group=None, target=None, name=None,
                           args=(), kwargs={}):
            self.inputqueue = Queue()
            self.idletime = time()
            super(_PollingThread, self).__init__(group,
                        target, name, args, kwargs)
        def add_pipe(self, buff, pipe, mode):
            self.inputqueue.put((buff, pipe, mode))
        def run(self):
            poller = kqueue()
            fds = {}
            events = []
            while True:
                while not self.inputqueue.empty():
                    # loop through the queue and gather new pipes to add the
                    # kernel queue
                    buff, pipe, mode = self.inputqueue.get()
                    if 'r' in mode:
                        events.append(kevent(pipe, KQ_FILTER_READ, KQ_EV_ADD))
                    elif 'w' in mode:
                        events.append(kevent(pipe, KQ_FILTER_WRITE, KQ_EV_ADD))
                    else:
                        continue
                    fds[pipe.fileno()] = (weakref.ref(buff), pipe)

                if len(events) == 0:
                    events = None
                events = poller.control(events, 16, 0.1)

                for i in range(len(events)):
                    # loop through response and handle events
                    event = events.pop()
                    buff, pipe = fds[event.ident]

                    if buff() is None:
                        # buffer object has closed out from underneath us
                        # pipe will be automatically removed from kqueue
                        pipe.close()
                        del fds[event.ident]
                        continue

                    if (abs(event.filter) & abs(KQ_FILTER_READ)) and event.data:
                        # new data has come in, push into the buffer
                        buff().write(pipe.read(event.data))

                    if (abs(event.filter) & abs(KQ_FILTER_WRITE)) and event.data:
                        # space is available to write data
                        pipe.write(buff().read(\
                                    min(buff()._nbytes, event.data, 2**16)))

                    if abs(event.flags) & abs(KQ_EV_EOF):
                        # pipe has been closed and all IO has been processed
                        # pipe will be automatically removed from kqueue
                        buff().close()
                        pipe.close()
                        del fds[event.ident]

                if len(fds) == 0:
                    # no pipes referenced
                    if self.idletime + 20 < time():
                        # idle timeout reached, terminate
                        break
                    sleep(0.1)
                else:
                    self.idletime = time()

class DequeBuffer( object ):
    """
    This is a chunked buffer, storing a sequence of buffer objects in a
    deque, allowing for FIFO operations outside the limited 64K system
    buffer, and the efficient freeing of memory without needing to rewrite
    a large contiguous buffer object.
    """
    class _Buffer( object ):
        """
        This subclass contains a buffer object and a read/write lock, as
        well as independent read and write positions.
        """
        __slots__ = ['buffer', 'lock', 'blocksize', 'EOF', 'rpos', 'wpos']
        def __init__(self, size=2**18):
            self.buffer = StringIO()
            self.lock = Lock()
            self.blocksize = size
            self.EOF = False
            self.rpos = 0
            self.wpos = 0

        def __del__(self):
            self.buffer.close()

        def read(self, nbytes):
            with self.lock:
                self.buffer.seek(self.rpos)
                buff = self.buffer.read(nbytes)
                self.rpos = self.buffer.tell()
                if self.rpos == self.blocksize:
                    self.EOF = True
                return buff

        def write(self, data):
            with self.lock:
                nbytes = self.blocksize-self.wpos
                if nbytes < len(data):
                    data = data[:nbytes]
                else:
                    nbytes = len(data)
                self.buffer.seek(self.wpos)
                self.buffer.write(data)
                self.wpos += nbytes
            return nbytes

        def rollback(self, nbytes):
            with self.lock:
                self.EOF = False
                if self.rpos < nbytes:
                    nbytes -= self.rpos
                    self.rpos = 0
                    return nbytes
                else:
                    self.rpos -= nbytes
                    return 0

        def close(self):
            with self.lock:
                self.buffer.close()

    _pollingthread = None

    def __init__(self, data=None, inp=None, out=None):
        self._nbytes = 0
        self._buffer = deque()
        self._rollback_pool = []
        self._rpipe = None
        self._wpipe = None
        self._closed = False
        if data:
            self.write(data)
        if inp:
            self.attach_input(inp)
        if out:
            self.attach_output(out)

    def __len__(self):
        return self._nbytes

    def read(self, nbytes=None):
        """
        Read up to specified amount from buffer, or whatever is available. 
        """
        # flush existing buffer
        self._rollback_pool = []
        data = StringIO()
        while True:
            try:
                # get first item, or return if no more blocks are avaialable
                tmp = self._buffer[0]
            except IndexError:
                break

            if nbytes:
                # read only what is requested
                data.write(tmp.read(nbytes-data.tell()))
            else:
                # read all that is available
                data.write(tmp.read(tmp.blocksize))

            if tmp.EOF:
                # block is exhausted, cycle it into the rollback pool
                self._rollback_pool.append(self._buffer.popleft())
            else:
                # end of data or request reached, return
                break
        self._nbytes -= data.tell()
        return data.getvalue()

    def write(self, data):
        """Write provide data into buffer."""
        data = StringIO(data)
        data.seek(0, 2)
        end = data.tell()
        pos = 0
        while True:
            data.seek(pos)
            try:
                # grab last entry in buffer
                tmp = self._buffer[-1]
            except IndexError:
                # buffer is empty, add a new block
                tmp = self._Buffer()
                self._buffer.append(tmp)
            # write as much data as possible, and update progress
            pos += tmp.write(data.read(tmp.blocksize))
            if pos == end:
                # no more data to write
                break
            else:
                # add another block, and loop back through
                self._buffer.append(self._Buffer())
        self._nbytes += end
        return end

    def _rollback(self, nbytes):
        """
        Roll back buffer specified number of bytes, pulling from a pool of
        expired blocks if needed. Pool will be flushed at the beginning of
        each read.
        """
        orig, nbytes = nbytes, self._buffer[0].rollback(nbytes)
        while nbytes:
            try:
                tmp = self._rollback_pool.pop()
            except IndexError:
                raise RuntimeError(('Tried to roll back {0} bytes into '+\
                        'DequeBuffer, but only {1} was available')\
                            .format(orig, orig-nbytes))
            else:
                self._buffer.appendleft(tmp)
                nbytes = tmp.rollback(nbytes)
        self._nbytes += nbytes

    def flush(self):
        pass

    def close(self):
        self._rpipe = None
        self._wpipe = None
        self._closed = True

    def attach_input(self, pipe):
        if self._rpipe is not None:
            raise RuntimeError('DequeBuffer is already connected to a '+\
                        'readable pipe')
        self._rpipe = pipe
        self._add_pipe(pipe, self, 'r')

    def attach_output(self, pipe):
        if self._wpipe is not None:
            raise RuntimeError('DequeBuffer is already connected to a '+\
                        'writable pipe')
        self._wpipe = pipe
        self._add_pipe(pipe, self, 'w')

    @classmethod
    def _add_pipe(cls, pipe, buffer, mode=None):
        """
        Add new pollable to the IO thread. Create new thread if necessary.
        Restart thread if it has terminated due to idle.
        """
        if not hasattr(pipe, 'fileno'):
            raise RuntimeError('DequeBuffer can only be attached to objects '+\
                    'with a fileno() method')
        if mode is None:
            # get IO mode from pipe
            mode = pipe.mode

        if (cls._pollingthread is None) or not cls._pollingthread.isAlive():
            # create new thread, and set it to not block shutdown
            cls._pollingthread = _PollingThread()
            cls._pollingthread.daemon = True
            cls._pollingthread.start()
        cls._pollingthread.add_pipe(buffer, pipe, mode)

class QuickProperty( object ):
    def __init__(self, maskedvar, default=None, handler=None):
        self.varname = maskedvar
        self.default = default
        if handler is None:
            handler = lambda x: x
        self.handler = handler

    def __get__(self, inst, owner):
        if inst is None:
            return self
        if hasattr(inst, self.varname):
            return getattr(inst, self.varname)
        return self.default

    def __set__(self, inst, value):
        try:
            value = self.handler(value)
        except:
            pass
        else:
            setattr(inst, self.varname, value)

    def __call__(self, handler):
        self.handler = handler
        return self

    def isDefault(self, inst):
        if hasattr(inst, self.varname):
            return False
        return True
