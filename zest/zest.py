"""
A function-oriented testing framework for Python 3.
See README.
"""
import time
import inspect
import types
from functools import wraps
from contextlib import contextmanager
from random import shuffle


def _get_class_that_defined_method(meth):
    # From https://stackoverflow.com/questions/3589311/get-defining-class-of-unbound-method-object-in-python-3/25959545#25959545
    if inspect.ismethod(meth):
        for cls in inspect.getmro(meth.__self__.__class__):
            if cls.__dict__.get(meth.__name__) is meth:
                return cls
        meth = meth.__func__

    if inspect.isfunction(meth):
        cls = getattr(
            inspect.getmodule(meth),
            meth.__qualname__.split(".<locals>", 1)[0].rsplit(".", 1)[0],
        )
        if isinstance(cls, type):
            return cls
        else:
            return inspect.getmodule(meth)


class TrappedException(Exception):
    """
    This will be passed back from a with zest.raises(SomeException) as e.
    It has one parameter: exception.

    Example:
        with zest.raises(SomeException) as e:
            something()
        assert e.exception.property == 1
    """

    pass


class MockFunction:
    def __init__(self, replacing_func=None):
        if replacing_func is not None:
            self.arg_spec = inspect.getfullargspec(replacing_func)
        else:
            self.arg_spec = None
        self.list_of_exceptions_to_raise = None
        self.exception_to_raise = None
        self.list_of_values_to_return = None
        self.value_to_return = None
        self.hook_to_call = None
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls += [(args, kwargs)]

        if self.hook_to_call is not None:
            return self.hook_to_call(*args, **kwargs)

        # EXCEPTION from series or repeatedly if requested
        if self.list_of_exceptions_to_raise is not None:
            if len(self.list_of_exceptions_to_raise) == 0:
                raise AssertionError(
                    "mock was called more times than the list_of_exceptions_to_raise had elements"
                )
            raise self.list_of_exceptions_to_raise.pop(0)

        if self.exception_to_raise is not None:
            raise self.exception_to_raise

        # RETURN from series or repeatedly
        if self.list_of_values_to_return is not None:
            if len(self.list_of_values_to_return) == 0:
                raise AssertionError(
                    "mock was called more times than the list_of_values_to_return had elements"
                )
            return self.list_of_values_to_return.pop(0)
        return self.value_to_return

    @property
    def n_calls(self):
        return len(self.calls)

    def reset(self):
        self.calls = []

    def hook(self, fn_to_call):
        self.hook_to_call = fn_to_call

    def returns(self, value_to_return):
        self.value_to_return = value_to_return

    def returns_serially(self, list_of_values_to_return):
        self.list_of_values_to_return = list_of_values_to_return

    def exceptions(self, exception_to_raise):
        self.exception_to_raise = exception_to_raise

    def exceptions_serially(self, list_of_exceptions_to_raise):
        self.list_of_exceptions_to_raise = list_of_exceptions_to_raise

    def called_once_with(self, *args, **kwargs):
        return (
            len(self.calls) == 1
            and self.calls[0][0] == args
            and self.calls[0][1] == kwargs
        )

    def called(self):
        return len(self.calls) > 0

    def called_once(self):
        return len(self.calls) == 1

    def not_called(self):
        return len(self.calls) == 0

    def normalized_calls(self):
        """
        Converts the calls into a list of kwargs by combining the args and kwargs.
        This simplifies assert handling in many cases where you don't care if
        the arguments were passed by position of name.
        """
        arg_spec = [arg for arg in self.arg_spec.args if arg != "self"]

        # arg_spec is now a list of all positional argument names that the real function
        # expects (excluding special *, **)

        normalized_calls = []
        for by_pos, by_keyword in self.calls:
            # COVERT all the arguments that were passed in without keywords...
            normalized_args = {
                arg_spec[i]: passed_value for i, passed_value in enumerate(by_pos)
            }

            # ADD in those arguments that were passed by keyword.
            normalized_args.update(by_keyword)

            normalized_calls += [normalized_args]

        return normalized_calls

    def normalized_call(self):
        """Used when you expect only one call and are checking some argument"""
        assert self.n_calls == 1
        return self.normalized_calls()[0]

    def called_once_with_kws(self, **kws):
        """
        Returns True if the mocked was called only once and with its args and kwargs
        normalized into the kws specified as the arguments to this func.
        """
        if self.n_calls != 1:
            return False

        return kws == self.normalized_calls()[0]


class zest:
    """
    This is a helper to make calling a little bit cleaner

    from common.zest.zest import zest

    def some_test():
        def test_1():
            with zest.raises(TypeError):
                example.call_some_global_func()

        def test_2():
            with zest.mock(example.some_global_func):
                example.some_global_func()

        zest()

    zest(some_test)
    """

    _call_log = []
    _call_stack = []
    _call_errors = []
    _call_warnings = []
    _call_tree = []
    _test_start_callback = None
    _test_stop_callback = None
    _mock_stack = []
    _allow_to_run = None
    _disable_shuffle = False

    @staticmethod
    def reset():
        zest._call_log = []
        zest._call_stack = []
        zest._call_errors = []
        zest._call_warnings = []
        zest._call_tree = []
        zest._test_start_callback = None
        zest._test_stop_callback = None
        zest._mock_stack = []
        zest._allow_to_run = None

    @staticmethod
    def skip(code="s", reason=None):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)

            setattr(wrapper, "skip", True)
            setattr(wrapper, "skip_code", code)
            setattr(wrapper, "skip_reason", reason)
            return wrapper

        return decorator

    @staticmethod
    def group(name):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)

            setattr(wrapper, "group", name)
            return wrapper

        return decorator

    @staticmethod
    def _setup_mock(symbol, substitute_fn=None):
        if not callable(symbol):
            raise AssertionError(f"Unmockable symbol {symbol} (must be callable)")

        old = None
        klass = None
        if substitute_fn is not None:
            new = substitute_fn
        else:
            new = MockFunction(symbol)
        klass = _get_class_that_defined_method(symbol)
        if isinstance(klass, types.ModuleType):
            frame = inspect.currentframe()
            module = inspect.getmodule(frame.f_back.f_back)
            for name, obj in inspect.getmembers(module):
                if (
                    hasattr(obj, "__qualname__")
                    and obj.__qualname__ == symbol.__qualname__
                ):
                    raise AssertionError(
                        f"You are mocking the module-level symbol {symbol.__qualname__} which "
                        f"is imported directly into the test module. You should instead "
                        f"import the containing module and then mock the sub-symbol."
                    )

        old = getattr(klass, symbol.__name__)
        setattr(klass, symbol.__name__, new)
        return old, klass, new

    @staticmethod
    def _clear_stack_mocks():
        for (klass, symbol, old, new, reset_before_each) in zest._mock_stack[-1]:
            setattr(klass, symbol.__name__, old)

    @staticmethod
    def stack_mock(
        symbol,
        reset_before_each=False,
        returns=None,
        returns_serially=None,
        substitute_fn=None,
    ):
        old, klass, new = zest._setup_mock(symbol, substitute_fn=substitute_fn)
        if returns is not None:
            new.returns(returns)
        elif returns_serially is not None:
            new.returns_serially(returns_serially)
        zest._mock_stack[-1] += [(klass, symbol, old, new, reset_before_each)]
        return new

    @staticmethod
    @contextmanager
    def mock(symbol, returns=None):
        old, klass, new = None, None, None
        try:
            old, klass, new = zest._setup_mock(symbol)
            if returns is not None:
                new.returns(returns)
            yield new
        finally:
            if klass and old:
                setattr(klass, symbol.__name__, old)

    @staticmethod
    @contextmanager
    def raises(expected_exception=Exception, **kwargs):
        """
        Use this in the inner most test. Do not attempt to encapsulate
        more than one test with this context.

        Good:
            def my_test_1():
                with zest.raises(ValueError):
                    something_that_raises()

            def my_test_2():
                with zest.raises(ValueError):
                    something_that_raises()

        Bad:
            with zest.raises(ValueError):
                def my_test_1():
                    something_that_raises()

                def my_test_2():
                    something_that_raises()


        Note, when asserting on properties of the exception
        be sure to do this outside the scope of the with as follows.
        (Note the reference to "e.exception." as opposed to "e."

        Good:
            with zest.raises(SomeException) as e:
                something_that_raises()
            assert e.exception.property == "something"

        Bad:
            with zest.raises(SomeException) as e:
                something_that_raises()
                assert e.exception.property == "something"  # This will NOT be run!

            with zest.raises(SomeException) as e:
                something_that_raises()

            # e is of type TrappedException. This following wil not work
            # because it does not check the e.exception property.
            assert e.property == "something"

        You can assert keys like this:

            with zest.raises(SomeException, property="something") as e:
                something_that_raises()
            # The above zest.raises will fail if the exception does not have
            # a key "property" that equals "something"

            with zest.raises(SomeException, in_property="something") as e:
                something_that_raises()
            # The above zest.rasises will fail if the exception does not have
            # a key "property" that CONTAINS the string "something"

        """
        got_expected_exception = False
        trapped_exception = TrappedException()
        try:
            yield trapped_exception
        except expected_exception as actual_e:
            trapped_exception.exception = actual_e
            got_expected_exception = True

        if got_expected_exception:
            # Check keys in the exception
            for key, val in kwargs.items():
                if key.startswith("in_"):
                    key = key[3:]
                    if val not in getattr(trapped_exception.exception, key):
                        raise AssertionError(
                            f"expected exception to have '{val}' in key '{key}'. "
                            f"Found '{getattr(trapped_exception.exception, key)}'"
                        )
                else:
                    if val != getattr(trapped_exception.exception, key):
                        raise AssertionError(
                            f"expected exception to have '{key}' == '{val}'. "
                            f"Found '{getattr(trapped_exception.exception, key)}'"
                        )
        else:
            raise AssertionError(
                f"expected {expected_exception} but nothing was raised."
            )

        # If some other exception was raised that should just bubble as usual

    @staticmethod
    def do(*funcs, test_start_callback=None, test_stop_callback=None):
        """
        This is the entrypoint of any zest at any depth.

        It is called by zest_runner in the case of "root" level
        tests. But each of those tests can call this recursively.

        Eg:

            def zest_test1():  # <-- This is the root level recursion called from zest_runner
                def it_does_x():
                    a = b()
                    assert a == 1

                def it_does_y():
                    a = c()
                    assert a == 2

                    def it_does_y1():
                        assert something()

                    zest()  # <-- This is the second sub-root level recursion

                zest()  # <-- This is the first sub-root level recursion

        This function works by looking up the call stack and analyzing
        the caller's scope to find functions that do not start with underscore
        and for two special underscore function: _before and _after...

        Call _before() (if defined) before each test
        Call _after() (if defined) after each test

        The class member _allow_to_run potentialy contains a list of
        zests that are allowed to execute in dotted form. Eg using above:
            ["zest_test1.it_does_y.it_does_y1"]

        This example would mean that "zest_test1" would run and "zest_test1.it_does_y"
        and "zest_test1.it_does_y.it_does_y1"

        When a parent level is given, all its children will run too.
        Eg: ["zest_test1.it_does_y"] means that it_does_y1 will run too.

        """
        if test_start_callback is not None:
            zest._test_start_callback = test_start_callback
        if test_stop_callback is not None:
            zest._test_stop_callback = test_stop_callback

        callers_special_local_funcs = {}

        if len(funcs) > 0:
            funcs_to_call = [
                (func.__name__, func)
                for func in funcs
                if isinstance(func, types.FunctionType)
                and not func.__name__.startswith("_")
            ]
        else:
            # Extract test functions from caller's scope
            frame = inspect.currentframe()
            try:
                zest_module_name = inspect.getmodule(frame).__name__
                while inspect.getmodule(frame).__name__ == zest_module_name:
                    frame = frame.f_back

                context = frame.f_locals

                callers_special_local_funcs = {
                    name: func
                    for name, func in context.items()
                    if isinstance(func, types.FunctionType)
                    and name.startswith("_")
                    and not isinstance(func, MockFunction)
                }

                funcs_to_call = [
                    (name, func)
                    for name, func in context.items()
                    if isinstance(func, types.FunctionType)
                    and not name.startswith("_")
                    and not isinstance(func, MockFunction)
                ]
            finally:
                del frame

        # Randomly shuffle test order to reveal accidental order dependencies.
        # TASK: make this a flag that is called during staging (w/ multi-run)
        funcs_to_call = sorted(funcs_to_call, key=lambda x: x[0])
        if len(funcs_to_call) > 1:
            if not zest._disable_shuffle:
                shuffle(funcs_to_call)

        _begin = callers_special_local_funcs.get("_begin")
        if _begin is not None:
            raise ValueError("A _begin function was declared. Maybe you meant _before?")

        for name, func in funcs_to_call:
            if len(zest._mock_stack) > 0:
                for mock_tuple in zest._mock_stack[-1]:
                    if mock_tuple[4]:  # if reset_before_each is set
                        mock_tuple[3].reset()  # Tell the mock to reset

            _before = callers_special_local_funcs.get("_before")
            if _before:
                try:
                    _before()
                except Exception as e:
                    zest._call_errors += [(e, zest._call_stack.copy())]
                    s = (
                        f"There was an exception while running '_before()' in test '{name}'. "
                        f"This may mean that the sub-tests are not enumerated and therefore can not be run."
                    )
                    zest._call_warnings += [s]

            zest._call_stack += [name]

            full_name = ".".join(zest._call_stack)
            if zest._allow_to_run is not None and full_name not in zest._allow_to_run:
                zest._call_stack.pop()
                continue

            zest._call_tree += [full_name]
            zest._call_log += [name]

            if zest._test_start_callback:
                zest._test_start_callback(name, call_stack=zest._call_stack, func=func)

            error = None
            start_time = time.time()
            try:
                if not hasattr(func, "skip"):
                    zest._mock_stack += [[]]
                    func()
                    zest._clear_stack_mocks()
                    zest._mock_stack.pop()
            except Exception as e:
                error = e
                zest._call_errors += [(e, zest._call_stack.copy())]
            finally:
                stop_time = time.time()
                if zest._test_stop_callback:
                    zest._test_stop_callback(
                        name,
                        call_stack=zest._call_stack,
                        error=error,
                        elapsed=stop_time - start_time,
                        func=func,
                    )
                zest._call_stack.pop()

            _after = callers_special_local_funcs.get("_after")
            if _after:
                _after()

    def __init__(self, *args, **kwargs):
        self.do(*args, **kwargs)