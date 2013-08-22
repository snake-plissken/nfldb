from __future__ import absolute_import, division, print_function
import datetime

import re
import sys

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import TRANSACTION_STATUS_INTRANS
from psycopg2.extensions import new_type, register_type

import pytz

import toml

import nflgame

__pdoc__ = {}

api_version = 2
__pdoc__['api_version'] = \
    """
    The schema version that this library corresponds to. When the schema
    version of the database is less than this value, `nfldb.connect` will
    automatically update the schema to the latest version before doing
    anything else.
    """


def connect(database=None, user=None, password=None, host=None, port=None,
            timezone=None):
    """
    Returns a `psycopg2._psycopg.connection` object from the
    `psycopg2.connect`. If database is `None`, then `connect` will look
    for a configuration file at `$XDG_CONFIG_HOME/nfldb/config.toml`
    with the database connection information. Otherwise, the connection
    will use the parameters given.

    This function will also compare the current schema version of the
    database against the API version `nfldb.api_version` and assert
    that they are equivalent. If the schema library version is less
    than the the API version, then the schema will be automatically
    upgraded. If the schema version is newer than the library version,
    then this function will raise an assertion error.  An assertion
    error will also be raised if the schema version is 0 and the
    database is not empty.

    N.B. The `timezone` parameter should be set to a value that
    PostgreSQL will accept. Select from the `pg_timezone_names` view
    to get a list of valid time zones. (Like the other parameters,
    when `database` is `None`, the `timezone` parameter is filled from
    `config.toml`.)
    """
    if database is None:
        try:
            conf = toml.loads(open('config.toml').read())
        except:
            print >> sys.stderr, "Invalid configuration file format."
            sys.exit(1)
        database = conf['pgsql'].get('database', None)
        user = conf['pgsql'].get('user', None)
        password = conf['pgsql'].get('password', None)
        host = conf['pgsql'].get('host', None)
        port = conf['pgsql'].get('port', None)
        timezone = conf.get('timezone', 'US/Eastern')
    conn = psycopg2.connect(database=database, user=user, password=password,
                            host=host, port=port,
                            cursor_factory=RealDictCursor)
    set_timezone(conn, timezone)

    # Start the migration. Make sure if this is the initial setup that
    # the DB is empty.
    sversion = schema_version(conn)
    assert sversion <= api_version, \
        'Library with version %d is older than the schema with version %d' \
        % (api_version, sversion)
    assert sversion > 0 or (sversion == 0 and _is_empty(conn)), \
        'Schema has version 0 but is not empty.'
    _migrate(conn, api_version)

    # Bind SQL -> Python casting functions.
    from nfldb.types import Clock, _Enum, Enums, FieldPosition, PossessionTime
    _bind_type(conn, 'game_phase', _Enum._pg_cast(Enums.game_phase))
    _bind_type(conn, 'season_phase', _Enum._pg_cast(Enums.season_phase))
    _bind_type(conn, 'game_day', _Enum._pg_cast(Enums.game_day))
    _bind_type(conn, 'playerpos', _Enum._pg_cast(Enums.playerpos))
    _bind_type(conn, 'game_time', Clock._pg_cast)
    _bind_type(conn, 'pos_period', PossessionTime._pg_cast)
    _bind_type(conn, 'field_pos', FieldPosition._pg_cast)

    return conn


def schema_version(conn):
    """
    Returns the schema version of the given database. If the version
    is not stored in the database, then `0` is returned.
    """
    with Tx(conn) as c:
        try:
            c.execute('SELECT value FROM meta WHERE name = %s', ['version'])
        except psycopg2.ProgrammingError:
            conn.rollback()
            return 0
        if c.rowcount == 0:
            return 0
        return int(c.fetchone()['value'])


def set_timezone(conn, timezone):
    """
    Sets the timezone for which all datetimes will be displayed
    as. Valid values are exactly the same set of values accepted
    by PostgreSQL. (Select from the `pg_timezone_names` view to
    get a list of valid time zones.)

    Note that all datetimes are stored in UTC. This setting only
    affects how datetimes are viewed from select queries.
    """
    with Tx(conn) as c:
        c.execute('SET timezone = %s', (timezone,))


def now():
    """
    Returns the current date/time in UTC. It can be used to compare
    against date/times in any of the `nfldb` objects.
    """
    return datetime.datetime.now(pytz.utc)


def _bind_type(conn, sql_type_name, cast):
    """
    Binds a `cast` function to the SQL type in the connection `conn`
    given by `sql_type_name`. `cast` must be a function with two
    parameters: the SQL value and a cursor object. It should return the
    appropriate Python object.

    Note that `sql_type_name` is not escaped.
    """
    with Tx(conn) as c:
        c.execute('SELECT NULL::%s' % sql_type_name)
        typ = new_type((c.description[0].type_code,), sql_type_name, cast)
        register_type(typ)


def _db_name(conn):
    m = re.search('dbname=(\S+)', conn.dsn)
    return m.group(1)


def _is_empty(conn):
    """
    Returns `True` if and only if there are no tables in the given
    database.
    """
    with Tx(conn) as c:
        c.execute('''
            SELECT COUNT(*) AS count FROM information_schema.tables
            WHERE table_catalog = %s AND table_schema = 'public'
        ''', [_db_name(conn)])
        if c.fetchone()['count'] == 0:
            return True
    return False


def _mogrify(cursor, xs):
    """Shortcut for mogrifying a list as if it were a tuple."""
    return cursor.mogrify('%s', (tuple(xs),))


class Tx (object):
    """
    Tx is a `with` compatible class that abstracts a transaction given
    a connection. If an exception occurs inside the `with` block, then
    rollback is automatically called. Otherwise, upon exit of the with
    block, commit is called.

    Tx blocks can be nested inside other Tx blocks. Nested Tx blocks
    never commit or rollback a transaction. Instead, the exception is
    passed along to the caller. Only the outermost transaction will
    commit or rollback the entire transaction.

    Use it like so:

        #!python
        with Tx(conn) as cursor:
            ...

    Which is meant to be roughly equivalent to the following:

        #!python
        with conn:
            with conn.cursor() as curs:
                ...
    """
    def __init__(self, psycho_conn):
        tstatus = psycho_conn.get_transaction_status()
        self.__nested = tstatus == TRANSACTION_STATUS_INTRANS
        self.__conn = psycho_conn
        self.__cursor = None

    def __enter__(self):
        self.__cursor = self.__conn.cursor()
        return self.__cursor

    def __exit__(self, typ, value, traceback):
        if not self.__cursor.closed:
            self.__cursor.close()
        if typ is not None:
            if not self.__nested:
                self.__conn.rollback()
            return False
        else:
            if not self.__nested:
                self.__conn.commit()
            return True


def _big_insert(cursor, table, datas):
    """
    Given a database cursor, table name and a list of asssociation
    lists of data (column name and value), perform a single large
    insert. Namely, each association list should correspond to a single
    row in `table`.

    Each association list must have exactly the same number of columns
    in exactly the same order.
    """
    stamped = table in ('game', 'drive', 'play')
    insert_fields = [k for k, _ in datas[0]]
    if stamped:
        insert_fields.append('time_inserted')
        insert_fields.append('time_updated')
    insert_fields = ', '.join(insert_fields)

    def times(xs):
        if stamped:
            xs.append('NOW()')
            xs.append('NOW()')
        return xs

    def vals(xs):
        return [v for _, v in xs]
    values = ', '.join(_mogrify(cursor, times(vals(data))) for data in datas)

    cursor.execute('''
        INSERT INTO %s (%s) VALUES %s
    ''' % (table, insert_fields, values))


def _upsert(cursor, table, data, pk):
    """
    Performs an arbitrary "upsert" given a table, an association list
    mapping key to value, and an association list representing the
    primary key.

    Note that this is **not** free of race conditions. It is the
    caller's responsibility to avoid race conditions. (e.g., By using a
    table or row lock.)

    If the table is `game`, `drive` or `play`, then the `time_insert`
    and `time_updated` fields are automatically populated.
    """
    stamped = table in ('game', 'drive', 'play')
    update_set = ['%s = %s' % (k, '%s') for k, _ in data]
    if stamped:
        update_set.append('time_updated = NOW()')
    update_set = ', '.join(update_set)

    insert_fields = [k for k, _ in data]
    insert_places = ['%s' for _ in data]
    if stamped:
        insert_fields.append('time_inserted')
        insert_fields.append('time_updated')
        insert_places.append('NOW()')
        insert_places.append('NOW()')
    insert_fields = ', '.join(insert_fields)
    insert_places = ', '.join(insert_places)

    pk_cond = ' AND '.join(['%s = %s' % (k, '%s') for k, _ in pk])
    q = '''
        UPDATE %s SET %s WHERE %s;
    ''' % (table, update_set, pk_cond)
    q += '''
        INSERT INTO %s (%s)
        SELECT %s WHERE NOT EXISTS (SELECT 1 FROM %s WHERE %s)
    ''' % (table, insert_fields, insert_places, table, pk_cond)

    values = [v for _, v in data]
    pk_values = [v for _, v in pk]
    try:
        cursor.execute(q, values + pk_values + values + pk_values)
    except psycopg2.ProgrammingError as e:
        print(cursor.query)
        raise e


# What follows are the migration functions. They follow the naming
# convention "_migrate_{VERSION}" where VERSION is an integer that
# corresponds to the version that the schema will be after the
# migration function runs. Each migration function is only responsible
# for running the queries required to update schema. It does not
# need to update the schema version.
#
# The migration functions should accept a cursor as a parameter,
# which are created in the higher-order _migrate. In particular,
# each migration function is run in its own transaction. Commits
# and rollbacks are handled automatically.


def _migrate(conn, to):
    current = schema_version(conn)
    assert current <= to

    globs = globals()
    for v in xrange(current+1, to+1):
        fname = '_migrate_%d' % v
        with Tx(conn) as c:
            assert fname in globs, 'Migration function %d not defined.' % v
            globs[fname](c)
            c.execute("UPDATE meta SET value = %s WHERE name = 'version'", [v])


def _migrate_1(c):
    c.execute('''
        CREATE TABLE meta (
            name varchar (255) PRIMARY KEY,
            value varchar (1000) NOT NULL
        )
    ''')
    c.execute("INSERT INTO meta (name, value) VALUES ('version', '1')")


def _migrate_2(c):
    from nfldb.types import Enums, _play_categories, _player_categories

    # Create some types and common constraints.
    c.execute('''
        CREATE DOMAIN gameid AS character varying (10)
                          CHECK (char_length(VALUE) = 10)
    ''')
    c.execute('''
        CREATE DOMAIN usmallint AS smallint
                          CHECK (VALUE >= 0)
    ''')
    c.execute('''
        CREATE DOMAIN game_clock AS smallint
                          CHECK (VALUE >= 0 AND VALUE <= 900)
    ''')
    c.execute('''
        CREATE DOMAIN field_offset AS smallint
                          CHECK (VALUE >= -50 AND VALUE <= 50)
    ''')

    c.execute('''
        CREATE DOMAIN utctime AS timestamp with time zone
                          CHECK (EXTRACT(TIMEZONE FROM VALUE) = '0')
    ''')
    c.execute('''
        CREATE TYPE game_phase AS ENUM %s
    ''' % _mogrify(c, Enums.game_phase))
    c.execute('''
        CREATE TYPE season_phase AS ENUM %s
    ''' % _mogrify(c, Enums.season_phase))
    c.execute('''
        CREATE TYPE game_day AS ENUM %s
    ''' % _mogrify(c, Enums.game_day))
    c.execute('''
        CREATE TYPE playerpos AS ENUM %s
    ''' % _mogrify(c, Enums.playerpos))
    c.execute('''
        CREATE TYPE game_time AS (
            phase game_phase,
            elapsed game_clock
        )
    ''')
    c.execute('''
        CREATE TYPE pos_period AS (
            elapsed usmallint
        )
    ''')
    c.execute('''
        CREATE TYPE field_pos AS (
            pos field_offset
        )
    ''')

    # Create the team table and populate it.
    c.execute('''
        CREATE TABLE team (
            team_id character varying (3) NOT NULL,
            city character varying (50) NOT NULL,
            name character varying (50) NOT NULL,
            PRIMARY KEY (team_id)
        )
    ''')
    c.execute('''
        INSERT INTO team (team_id, city, name) VALUES %s
    ''' % (', '.join(_mogrify(c, team[0:3]) for team in nflgame.teams)))

    # Create the rest of the schema.
    c.execute('''
        CREATE TABLE player (
            player_id character varying (10) NOT NULL
                CHECK (char_length(player_id) = 10),
            gsis_name character varying (75) NOT NULL,
            full_name character varying (75) NULL,
            last_team character varying (3) NOT NULL,
            position playerpos NULL,
            PRIMARY KEY (player_id),
            FOREIGN KEY (last_team)
                REFERENCES team (team_id)
                ON DELETE RESTRICT
                ON UPDATE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE game (
            gsis_id gameid NOT NULL,
            gamekey character varying (5) NULL,
            start_time utctime NOT NULL,
            week usmallint NOT NULL
                CHECK (week >= 1 AND week <= 25),
            day_of_week game_day NOT NULL,
            season_year usmallint NOT NULL
                CHECK (season_year >= 1960 AND season_year <= 2100),
            season_type season_phase NOT NULL,
            finished boolean NOT NULL,
            home_team character varying (3) NOT NULL,
            home_score usmallint NOT NULL,
            home_score_q1 usmallint NULL,
            home_score_q2 usmallint NULL,
            home_score_q3 usmallint NULL,
            home_score_q4 usmallint NULL,
            home_score_q5 usmallint NULL,
            home_turnovers usmallint NOT NULL,
            away_team character varying (3) NOT NULL,
            away_score usmallint NOT NULL,
            away_score_q1 usmallint NULL,
            away_score_q2 usmallint NULL,
            away_score_q3 usmallint NULL,
            away_score_q4 usmallint NULL,
            away_score_q5 usmallint NULL,
            away_turnovers usmallint NOT NULL,
            time_inserted utctime NOT NULL,
            time_updated utctime NOT NULL,
            PRIMARY KEY (gsis_id),
            FOREIGN KEY (home_team)
                REFERENCES team (team_id)
                ON DELETE RESTRICT
                ON UPDATE CASCADE,
            FOREIGN KEY (away_team)
                REFERENCES team (team_id)
                ON DELETE RESTRICT
                ON UPDATE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE drive (
            gsis_id gameid NOT NULL,
            drive_id usmallint NOT NULL,
            start_field field_pos NULL,
            start_time game_time NOT NULL,
            end_field field_pos NULL,
            end_time game_time NOT NULL,
            pos_team character varying (3) NOT NULL,
            pos_time pos_period NULL,
            first_downs usmallint NOT NULL,
            result text NULL,
            penalty_yards smallint NOT NULL,
            yards_gained smallint NOT NULL,
            play_count usmallint NOT NULL,
            time_inserted utctime NOT NULL,
            time_updated utctime NOT NULL,
            PRIMARY KEY (gsis_id, drive_id),
            FOREIGN KEY (gsis_id)
                REFERENCES game (gsis_id)
                ON DELETE CASCADE,
            FOREIGN KEY (pos_team)
                REFERENCES team (team_id)
                ON DELETE RESTRICT
                ON UPDATE CASCADE
        )
    ''')

    # I've taken the approach of using a sparse table to represent
    # sparse play statistic data. See issue #2:
    # https://github.com/BurntSushi/nfldb/issues/2
    c.execute('''
        CREATE TABLE play (
            gsis_id gameid NOT NULL,
            drive_id usmallint NOT NULL,
            play_id usmallint NOT NULL,
            time game_time NOT NULL,
            pos_team character varying (3) NULL,
            yardline field_pos NULL,
            down smallint NULL
                CHECK (down >= 1 AND down <= 4),
            yards_to_go smallint NULL
                CHECK (yards_to_go >= 0 AND yards_to_go <= 100),
            description text NULL,
            note text NULL,
            time_inserted utctime NOT NULL,
            time_updated utctime NOT NULL,
            %s,
            PRIMARY KEY (gsis_id, drive_id, play_id),
            FOREIGN KEY (gsis_id, drive_id)
                REFERENCES drive (gsis_id, drive_id)
                ON DELETE CASCADE,
            FOREIGN KEY (gsis_id)
                REFERENCES game (gsis_id)
                ON DELETE CASCADE,
            FOREIGN KEY (pos_team)
                REFERENCES team (team_id)
                ON DELETE RESTRICT
                ON UPDATE CASCADE
        )
    ''' % ', '.join([cat._sql_field for cat in _play_categories.values()]))

    c.execute('''
        CREATE TABLE play_player (
            gsis_id gameid NOT NULL,
            drive_id usmallint NOT NULL,
            play_id usmallint NOT NULL,
            player_id character varying (10) NOT NULL,
            %s,
            PRIMARY KEY (gsis_id, drive_id, play_id, player_id),
            FOREIGN KEY (gsis_id, drive_id, play_id)
                REFERENCES play (gsis_id, drive_id, play_id)
                ON DELETE CASCADE,
            FOREIGN KEY (gsis_id, drive_id)
                REFERENCES drive (gsis_id, drive_id)
                ON DELETE CASCADE,
            FOREIGN KEY (gsis_id)
                REFERENCES game (gsis_id)
                ON DELETE CASCADE,
            FOREIGN KEY (player_id)
                REFERENCES player (player_id)
                ON DELETE RESTRICT
        )
    ''' % ', '.join(cat._sql_field for cat in _player_categories.values()))

    # Now create all of the indexes.
    c.execute('''
        CREATE INDEX player_in_gsis_name ON player (gsis_name ASC);
        CREATE INDEX player_in_full_name ON player (full_name ASC);
        CREATE INDEX player_in_last_team ON player (last_team ASC);
        CREATE INDEX player_in_position ON player (position ASC);
    ''')
    c.execute('''
        CREATE INDEX game_in_gamekey ON game (gamekey ASC);
        CREATE INDEX game_in_finished ON game (finished ASC);
        CREATE INDEX game_in_home_team ON game (home_team ASC);
        CREATE INDEX game_in_away_team ON game (away_team ASC);
        CREATE INDEX game_in_home_score ON game (home_score ASC);
        CREATE INDEX game_in_away_score ON game (away_score ASC);
        CREATE INDEX game_in_home_turnovers ON game (home_turnovers ASC);
        CREATE INDEX game_in_away_turnovers ON game (away_turnovers ASC);
    ''')
    c.execute('''
        CREATE INDEX drive_in_gsis_id ON drive (gsis_id ASC);
        CREATE INDEX drive_in_drive_id ON drive (drive_id ASC);
        CREATE INDEX drive_in_start_field ON drive
            (((start_field).pos) ASC);
        CREATE INDEX drive_in_end_field ON drive
            (((end_field).pos) ASC);
        CREATE INDEX drive_in_start_time ON drive
            (((start_time).phase) ASC, ((start_time).elapsed) ASC);
        CREATE INDEX drive_in_end_time ON drive
            (((end_time).phase) ASC, ((end_time).elapsed) ASC);
        CREATE INDEX drive_in_pos_team ON drive (pos_team ASC);
        CREATE INDEX drive_in_pos_time ON drive
            (((pos_time).elapsed) DESC);
        CREATE INDEX drive_in_first_downs ON drive (first_downs DESC);
        CREATE INDEX drive_in_penalty_yards ON drive (penalty_yards DESC);
        CREATE INDEX drive_in_yards_gained ON drive (yards_gained DESC);
        CREATE INDEX drive_in_play_count ON drive (play_count DESC);
    ''')
    c.execute('''
        CREATE INDEX play_in_gsis_id ON play (gsis_id ASC);
        CREATE INDEX play_in_drive_id ON play (drive_id ASC);
        CREATE INDEX play_in_time ON play
            (((time).phase) ASC, ((time).elapsed) ASC);
        CREATE INDEX play_in_yardline ON play
            (((yardline).pos) ASC);
        CREATE INDEX play_in_down ON play (down ASC);
        CREATE INDEX play_in_yards_to_go ON play (yards_to_go DESC);
    ''')
    c.execute('''
        CREATE INDEX play_player_in_player_id ON play_player (player_id ASC);
    ''')
