from flightsql.exceptions import Error


def check_closed(f):
    def g(self, *args, **kwargs):
        if self.closed:
            raise Error(f"{self.__class__.__name__} already closed")
        return f(self, *args, **kwargs)

    return g


def translate_errors(f):
    """Re-raise PyArrow/Flight failures as PEP 249 exceptions.

    SQLAlchemy only wraps (and pool pre-ping only recycles on) exceptions that
    derive from the DB API ``Error``. The original exception is kept as the
    cause and its message is preserved.
    """
    import functools

    @functools.wraps(f)
    def g(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Error:
            raise
        except Exception as error:
            translated = dbapi_error(error)
            if translated is None:
                raise
            raise translated from error

    return g


def dbapi_error(error: Exception):
    import pyarrow as pa
    from pyarrow import flight

    from flightsql import exceptions as exc

    message = str(error)
    if isinstance(error, (flight.FlightUnavailableError, flight.FlightTimedOutError, flight.FlightCancelledError)):
        return exc.OperationalError(message)
    if isinstance(error, (flight.FlightUnauthenticatedError, flight.FlightUnauthorizedError)):
        return exc.OperationalError(message)
    if isinstance(error, pa.ArrowNotImplementedError):
        return exc.NotSupportedError(message)
    if isinstance(error, flight.FlightInternalError):
        return exc.InternalError(message)
    if isinstance(error, (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowKeyError, pa.ArrowIndexError)):
        return exc.ProgrammingError(message)
    if isinstance(error, (flight.FlightError, pa.ArrowException)):
        return exc.DatabaseError(message)
    return None
