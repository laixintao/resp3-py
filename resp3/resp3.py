"""
The original connection code is stolen from redis-py.
"""

from time import time
import errno
import io
import os
import socket
import sys

from .exceptions import (
    AuthenticationError,
    AuthenticationWrongNumberOfArgsError,
    BusyLoadingError,
    ConnectionError,
    DataError,
    ExecAbortError,
    InvalidResponse,
    NoPermissionError,
    NoScriptError,
    ReadOnlyError,
    RedisError,
    ResponseError,
    TimeoutError,
)

try:
    import ssl

    ssl_available = True
except ImportError:
    ssl_available = False


SYM_STAR = b"*"
SYM_DOLLAR = b"$"
SYM_CRLF = b"\r\n"
SYM_EMPTY = b""
SENTINEL = object()

SERVER_CLOSED_CONNECTION_ERROR = "Connection closed by server."

NONBLOCKING_EXCEPTION_ERROR_NUMBERS = {BlockingIOError: errno.EWOULDBLOCK}

if ssl_available:
    if hasattr(ssl, "SSLWantReadError"):
        NONBLOCKING_EXCEPTION_ERROR_NUMBERS[ssl.SSLWantReadError] = 2
        NONBLOCKING_EXCEPTION_ERROR_NUMBERS[ssl.SSLWantWriteError] = 2
    else:
        NONBLOCKING_EXCEPTION_ERROR_NUMBERS[ssl.SSLError] = 2

# In Python 2.7 a socket.error is raised for a nonblocking read.
# The _compat module aliases BlockingIOError to socket.error to be
# Python 2/3 compatible.
# However this means that all socket.error exceptions need to be handled
# properly within these exception handlers.
# We need to make sure socket.error is included in these handlers and
# provide a dummy error number that will never match a real exception.
if socket.error not in NONBLOCKING_EXCEPTION_ERROR_NUMBERS:
    NONBLOCKING_EXCEPTION_ERROR_NUMBERS[socket.error] = -999999
NONBLOCKING_EXCEPTIONS = tuple(NONBLOCKING_EXCEPTION_ERROR_NUMBERS.keys())


class SocketBuffer(object):
    def __init__(self, socket, socket_read_size, socket_timeout):
        self._sock = socket
        self.socket_read_size = socket_read_size
        self.socket_timeout = socket_timeout
        self._buffer = io.BytesIO()
        # number of bytes written to the buffer from the socket
        self.bytes_written = 0
        # number of bytes read from the buffer
        self.bytes_read = 0

    @property
    def length(self):
        return self.bytes_written - self.bytes_read

    def _read_from_socket(self, length=None, timeout=SENTINEL, raise_on_timeout=True):
        sock = self._sock
        socket_read_size = self.socket_read_size
        buf = self._buffer
        buf.seek(self.bytes_written)
        marker = 0
        custom_timeout = timeout is not SENTINEL

        try:
            if custom_timeout:
                sock.settimeout(timeout)
            while True:
                data = self._sock.recv(socket_read_size)
                # an empty string indicates the server shutdown the socket
                if isinstance(data, bytes) and len(data) == 0:
                    raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)
                buf.write(data)
                data_length = len(data)
                self.bytes_written += data_length
                marker += data_length

                if length is not None and length > marker:
                    continue
                return True
        except socket.timeout:
            if raise_on_timeout:
                raise TimeoutError("Timeout reading from socket")
            return False
        except NONBLOCKING_EXCEPTIONS as ex:
            # if we're in nonblocking mode and the recv raises a
            # blocking error, simply return False indicating that
            # there's no data to be read. otherwise raise the
            # original exception.
            allowed = NONBLOCKING_EXCEPTION_ERROR_NUMBERS.get(ex.__class__, -1)
            if not raise_on_timeout and ex.errno == allowed:
                return False
            raise ConnectionError("Error while reading from socket: %s" % (ex.args,))
        finally:
            if custom_timeout:
                sock.settimeout(self.socket_timeout)

    def can_read(self, timeout):
        return bool(self.length) or self._read_from_socket(
            timeout=timeout, raise_on_timeout=False
        )

    def read(self, length):
        length = length + 2  # make sure to read the \r\n terminator
        # make sure we've read enough data from the socket
        if length > self.length:
            self._read_from_socket(length - self.length)

        self._buffer.seek(self.bytes_read)
        data = self._buffer.read(length)
        self.bytes_read += len(data)

        # purge the buffer when we've consumed it all so it doesn't
        # grow forever
        if self.bytes_read == self.bytes_written:
            self.purge()

        return data[:-2]

    def readline(self):
        buf = self._buffer
        buf.seek(self.bytes_read)
        data = buf.readline()
        while not data.endswith(SYM_CRLF):
            # there's more data in the socket that we need
            self._read_from_socket()
            buf.seek(self.bytes_read)
            data = buf.readline()

        self.bytes_read += len(data)

        # purge the buffer when we've consumed it all so it doesn't
        # grow forever
        if self.bytes_read == self.bytes_written:
            self.purge()

        return data[:-2]

    def purge(self):
        self._buffer.seek(0)
        self._buffer.truncate()
        self.bytes_written = 0
        self.bytes_read = 0

    def close(self):
        try:
            self.purge()
            self._buffer.close()
        except Exception:
            # issue #633 suggests the purge/close somehow raised a
            # BadFileDescriptor error. Perhaps the client ran out of
            # memory or something else? It's probably OK to ignore
            # any error being raised from purge/close since we're
            # removing the reference to the instance below.
            pass
        self._buffer = None
        self._sock = None


class Encoder(object):
    "Encode strings to bytes-like and decode bytes-like to strings"

    def __init__(self, encoding, encoding_errors, decode_responses):
        self.encoding = encoding
        self.encoding_errors = encoding_errors
        self.decode_responses = decode_responses

    def encode(self, value):
        "Return a bytestring or bytes-like representation of the value"
        if isinstance(value, (bytes, memoryview)):
            return value
        elif isinstance(value, bool):
            # special case bool since it is a subclass of int
            raise DataError(
                "Invalid input of type: 'bool'. Convert to a "
                "bytes, string, int or float first."
            )
        elif isinstance(value, float):
            value = repr(value).encode()
        elif isinstance(value, (int, int)):
            # python 2 repr() on ints is '123L', so use str() instead
            value = str(value).encode()
        elif not isinstance(value, str):
            # a value we don't know how to deal with. throw an error
            typename = type(value).__name__
            raise DataError(
                "Invalid input of type: '%s'. Convert to a "
                "bytes, string, int or float first." % typename
            )
        if isinstance(value, str):
            value = value.encode(self.encoding, self.encoding_errors)
        return value

    def decode(self, value, force=False):
        "Return a str string from the bytes-like representation"
        if self.decode_responses or force:
            if isinstance(value, memoryview):
                value = value.tobytes()
            if isinstance(value, bytes):
                value = value.decode(self.encoding, self.encoding_errors)
        return value


class BaseParser(object):
    EXCEPTION_CLASSES = {
        "ERR": {
            "max number of clients reached": ConnectionError,
            "Client sent AUTH, but no password is set": AuthenticationError,
            "invalid password": AuthenticationError,
            # some Redis server versions report invalid command syntax
            # in lowercase
            "wrong number of arguments for 'auth' command": AuthenticationWrongNumberOfArgsError,
            # some Redis server versions report invalid command syntax
            # in uppercase
            "wrong number of arguments for 'AUTH' command": AuthenticationWrongNumberOfArgsError,
        },
        "EXECABORT": ExecAbortError,
        "LOADING": BusyLoadingError,
        "NOSCRIPT": NoScriptError,
        "READONLY": ReadOnlyError,
        "NOAUTH": AuthenticationError,
        "NOPERM": NoPermissionError,
    }

    def parse_error(self, response):
        "Parse an error response"
        error_code = response.split(" ")[0]
        if error_code in self.EXCEPTION_CLASSES:
            response = response[len(error_code) + 1 :]
            exception_class = self.EXCEPTION_CLASSES[error_code]
            if isinstance(exception_class, dict):
                exception_class = exception_class.get(response, ResponseError)
            return exception_class(response)
        return ResponseError(response)


class PythonParser(BaseParser):
    "Plain Python parsing class"

    def __init__(self, socket_read_size):
        self.socket_read_size = socket_read_size
        self.encoder = None
        self._sock = None
        self._buffer = None

    def __del__(self):
        self.on_disconnect()

    def on_connect(self, connection):
        "Called when the socket connects"
        self._sock = connection._sock
        self._buffer = SocketBuffer(
            self._sock, self.socket_read_size, connection.socket_timeout
        )
        self.encoder = connection.encoder

    def on_disconnect(self):
        "Called when the socket disconnects"
        self._sock = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None
        self.encoder = None

    def can_read(self, timeout):
        return self._buffer and self._buffer.can_read(timeout)

    def read_response(self):
        response = self._buffer.readline()
        if not response:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)

        byte, response = chr(response[0]), response[1:]

        if byte not in ("-", "+", ":", "$", "*"):
            raise InvalidResponse("Protocol Error: %s, %s" % (str(byte), str(response)))

        # server returned an error
        if byte == "-":
            error = self.parse_error(response)
            # if the error is a ConnectionError, raise immediately so the user
            # is notified
            if isinstance(error, ConnectionError):
                raise error
            # otherwise, we're dealing with a ResponseError that might beint
            # inside a pipeline response. the connection's read_response()
            # and/or the pipeline's execute() will raise this error if
            # necessary, so just return the exception instance here.
            return error
        # single value
        elif byte == "+":
            pass
        # int value
        elif byte == ":":
            response = int(response)
        # bulk response
        elif byte == "$":
            length = int(response)
            if length == -1:
                return None
            response = self._buffer.read(length)
        # multi-bulk response
        elif byte == "*":
            length = int(response)
            if length == -1:
                return None
            response = [self.read_response() for i in xrange(length)]
        if isinstance(response, bytes):
            response = self.encoder.decode(response)
        return response


class Connection(object):
    "Manages TCP communication to and from a Redis server"

    def __init__(
        self,
        host="localhost",
        port=6379,
        db=0,
        password=None,
        socket_timeout=None,
        socket_connect_timeout=None,
        socket_keepalive=False,
        socket_keepalive_options=None,
        socket_type=0,
        retry_on_timeout=False,
        encoding="utf-8",
        encoding_errors="strict",
        decode_responses=False,
        parser_class=PythonParser,
        socket_read_size=65536,
        health_check_interval=0,
        client_name=None,
        username=None,
    ):
        self.pid = os.getpid()
        self.host = host
        self.port = int(port)
        self.db = db
        self.username = username
        self.client_name = client_name
        self.password = password
        self.socket_timeout = socket_timeout
        self.socket_connect_timeout = socket_connect_timeout or socket_timeout
        self.socket_keepalive = socket_keepalive
        self.socket_keepalive_options = socket_keepalive_options or {}
        self.socket_type = socket_type
        self.retry_on_timeout = retry_on_timeout
        self.health_check_interval = health_check_interval
        self.next_health_check = 0
        self.encoder = Encoder(encoding, encoding_errors, decode_responses)
        self._sock = None
        self._parser = parser_class(socket_read_size=socket_read_size)
        self._connect_callbacks = []
        self._buffer_cutoff = 6000

    def __repr__(self):
        repr_args = ",".join(["%s=%s" % (k, v) for k, v in self.repr_pieces()])
        return "%s<%s>" % (self.__class__.__name__, repr_args)

    def repr_pieces(self):
        pieces = [("host", self.host), ("port", self.port), ("db", self.db)]
        if self.client_name:
            pieces.append(("client_name", self.client_name))
        return pieces

    def __del__(self):
        self.disconnect()

    def register_connect_callback(self, callback):
        self._connect_callbacks.append(callback)

    def clear_connect_callbacks(self):
        self._connect_callbacks = []

    def connect(self):
        "Connects to the Redis server if not already connected"
        if self._sock:
            return
        try:
            sock = self._connect()
        except socket.timeout:
            raise TimeoutError("Timeout connecting to server")
        except socket.error:
            e = sys.exc_info()[1]
            raise ConnectionError(self._error_message(e))

        self._sock = sock
        try:
            self.on_connect()
        except RedisError:
            # clean up after any error in on_connect
            self.disconnect()
            raise

        # run any user callbacks. right now the only internal callback
        # is for pubsub channel/pattern resubscription
        for callback in self._connect_callbacks:
            callback(self)

    def _connect(self):
        "Create a TCP socket connection"
        # we want to mimic what socket.create_connection does to support
        # ipv4/ipv6, but we want to set options prior to calling
        # socket.connect()
        err = None
        for res in socket.getaddrinfo(
            self.host, self.port, self.socket_type, socket.SOCK_STREAM
        ):
            family, socktype, proto, canonname, socket_address = res
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                # TCP_NODELAY
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                # TCP_KEEPALIVE
                if self.socket_keepalive:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    for k, v in self.socket_keepalive_options.items():
                        sock.setsockopt(socket.IPPROTO_TCP, k, v)

                # set the socket_connect_timeout before we connect
                sock.settimeout(self.socket_connect_timeout)

                # connect
                sock.connect(socket_address)

                # set the socket_timeout now that we're connected
                sock.settimeout(self.socket_timeout)
                return sock

            except socket.error as _:
                err = _
                if sock is not None:
                    sock.close()

        if err is not None:
            raise err
        raise socket.error("socket.getaddrinfo returned an empty list")

    def _error_message(self, exception):
        # args for socket.error can either be (errno, "message")
        # or just "message"
        if len(exception.args) == 1:
            return "Error connecting to %s:%s. %s." % (
                self.host,
                self.port,
                exception.args[0],
            )
        else:
            return "Error %s connecting to %s:%s. %s." % (
                exception.args[0],
                self.host,
                self.port,
                exception.args[1],
            )

    def on_connect(self):
        "Initialize the connection, authenticate and select a database"
        self._parser.on_connect(self)

        # if username and/or password are set, authenticate
        if self.username or self.password:
            if self.username:
                auth_args = (self.username, self.password or "")
            else:
                auth_args = (self.password,)
            # avoid checking health here -- PING will fail if we try
            # to check the health prior to the AUTH
            self.send_command("AUTH", *auth_args, check_health=False)

            try:
                auth_response = self.read_response()
            except AuthenticationWrongNumberOfArgsError:
                # a username and password were specified but the Redis
                # server seems to be < 6.0.0 which expects a single password
                # arg. retry auth with just the password.
                # https://github.com/andymccurdy/redis-py/issues/1274
                self.send_command("AUTH", self.password, check_health=False)
                auth_response = self.read_response()

            if auth_response != "OK":
                raise AuthenticationError("Invalid Username or Password")

        # if a client_name is given, set it
        if self.client_name:
            self.send_command("CLIENT", "SETNAME", self.client_name)
            if self.read_response() != "OK":
                raise ConnectionError("Error setting client name")

        # if a database is specified, switch to it
        if self.db:
            self.send_command("SELECT", self.db)
            if self.read_response() != "OK":
                raise ConnectionError("Invalid Database")

    def disconnect(self):
        "Disconnects from the Redis server"
        self._parser.on_disconnect()
        if self._sock is None:
            return
        try:
            if os.getpid() == self.pid:
                self._sock.shutdown(socket.SHUT_RDWR)
            self._sock.close()
        except socket.error:
            pass
        self._sock = None

    def check_health(self):
        "Check the health of the connection with a PING/PONG"
        if self.health_check_interval and time() > self.next_health_check:
            try:
                self.send_command("PING", check_health=False)
                if self.read_response() != "PONG":
                    raise ConnectionError("Bad response from PING health check")
            except (ConnectionError, TimeoutError):
                self.disconnect()
                self.send_command("PING", check_health=False)
                if self.read_response() != "PONG":
                    raise ConnectionError("Bad response from PING health check")

    def send_packed_command(self, command, check_health=True):
        "Send an already packed command to the Redis server"
        if not self._sock:
            self.connect()
        # guard against health check recursion
        if check_health:
            self.check_health()
        try:
            if isinstance(command, str):
                command = [command]
            for item in command:
                self._sock.sendall(item)
        except socket.timeout:
            self.disconnect()
            raise TimeoutError("Timeout writing to socket")
        except socket.error:
            e = sys.exc_info()[1]
            self.disconnect()
            if len(e.args) == 1:
                errno, errmsg = "UNKNOWN", e.args[0]
            else:
                errno = e.args[0]
                errmsg = e.args[1]
            raise ConnectionError(
                "Error %s while writing to socket. %s." % (errno, errmsg)
            )
        except:  # noqa: E722
            self.disconnect()
            raise

    def send_command(self, *args, **kwargs):
        "Pack and send a command to the Redis server"
        self.send_packed_command(
            self.pack_command(*args), check_health=kwargs.get("check_health", True)
        )

    def can_read(self, timeout=0):
        "Poll the socket to see if there's data that can be read."
        sock = self._sock
        if not sock:
            self.connect()
            sock = self._sock
        return self._parser.can_read(timeout)

    def read_response(self):
        "Read the response from a previously sent command"
        try:
            response = self._parser.read_response()
        except socket.timeout:
            self.disconnect()
            raise TimeoutError("Timeout reading from %s:%s" % (self.host, self.port))
        except socket.error:
            self.disconnect()
            e = sys.exc_info()[1]
            raise ConnectionError(
                "Error while reading from %s:%s : %s" % (self.host, self.port, e.args)
            )
        except:  # noqa: E722
            self.disconnect()
            raise

        if self.health_check_interval:
            self.next_health_check = time() + self.health_check_interval

        if isinstance(response, ResponseError):
            raise response
        return response

    def pack_command(self, *args):
        "Pack a series of arguments into the Redis protocol"
        output = []
        # the client might have included 1 or more literal arguments in
        # the command name, e.g., 'CONFIG GET'. The Redis server expects these
        # arguments to be sent separately, so split the first argument
        # manually. These arguments should be bytestrings so that they are
        # not encoded.
        if isinstance(args[0], str):
            args = tuple(args[0].encode().split()) + args[1:]
        elif b" " in args[0]:
            args = tuple(args[0].split()) + args[1:]

        buff = SYM_EMPTY.join((SYM_STAR, str(len(args)).encode(), SYM_CRLF))

        buffer_cutoff = self._buffer_cutoff
        for arg in map(self.encoder.encode, args):
            # to avoid large string mallocs, chunk the command into the
            # output list if we're sending large values or memoryviews
            arg_length = len(arg)
            if (
                len(buff) > buffer_cutoff
                or arg_length > buffer_cutoff
                or isinstance(arg, memoryview)
            ):
                buff = SYM_EMPTY.join(
                    (buff, SYM_DOLLAR, str(arg_length).encode(), SYM_CRLF)
                )
                output.append(buff)
                output.append(arg)
                buff = SYM_CRLF
            else:
                buff = SYM_EMPTY.join(
                    (
                        buff,
                        SYM_DOLLAR,
                        str(arg_length).encode(),
                        SYM_CRLF,
                        arg,
                        SYM_CRLF,
                    )
                )
        output.append(buff)
        return output

    def pack_commands(self, commands):
        "Pack multiple commands into the Redis protocol"
        output = []
        pieces = []
        buffer_length = 0
        buffer_cutoff = self._buffer_cutoff

        for cmd in commands:
            for chunk in self.pack_command(*cmd):
                chunklen = len(chunk)
                if (
                    buffer_length > buffer_cutoff
                    or chunklen > buffer_cutoff
                    or isinstance(chunk, memoryview)
                ):
                    output.append(SYM_EMPTY.join(pieces))
                    buffer_length = 0
                    pieces = []

                if chunklen > buffer_cutoff or isinstance(chunk, memoryview):
                    output.append(chunk)
                else:
                    pieces.append(chunk)
                    buffer_length += chunklen

        if pieces:
            output.append(SYM_EMPTY.join(pieces))
        return output

    def call(self, command, *args):
        self.send_command(command, *args)
        return self.read_response()
