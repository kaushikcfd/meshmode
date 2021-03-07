__copyright__ = "Copyright (C) 2020 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


from functools import partial
import numpy as np
import loopy as lp
from typing import Callable, Any
from loopy.version import MOST_RECENT_LANGUAGE_VERSION
from pytools import memoize_method

__doc__ = """
.. autofunction:: make_loopy_program
.. autoclass:: ArrayContext
.. autoclass:: PyOpenCLArrayContext
.. autofunction:: pytest_generate_tests_for_pyopencl_array_context
"""


def make_loopy_program(domains, statements, kernel_data=["..."],
        name="mm_actx_kernel"):
    """Return a :class:`loopy.LoopKernel` suitable for use with
    :meth:`ArrayContext.call_loopy`.
    """
    return lp.make_kernel(
            domains,
            statements,
            kernel_data=kernel_data,
            options=lp.Options(
                no_numpy=True,
                return_dict=True),
            # FIXME: Not sure why is this here.
            # default_offset=lp.auto,
            name=name,
            lang_version=MOST_RECENT_LANGUAGE_VERSION)


# {{{ ArrayContext

class _BaseFakeNumpyNamespace:
    def __init__(self, array_context):
        self._array_context = array_context
        self.linalg = self._get_fake_numpy_linalg_namespace()

    def _get_fake_numpy_linalg_namespace(self):
        return _BaseFakeNumpyLinalgNamespace(self.array_context)

    _numpy_math_functions = frozenset({
        # https://numpy.org/doc/stable/reference/routines.math.html

        # FIXME: Heads up: not all of these are supported yet.
        # But I felt it was important to only dispatch actually existing
        # numpy functions to loopy.

        # Trigonometric functions
        "sin", "cos", "tan", "arcsin", "arccos", "arctan", "hypot", "arctan2",
        "degrees", "radians", "unwrap", "deg2rad", "rad2deg",

        # Hyperbolic functions
        "sinh", "cosh", "tanh", "arcsinh", "arccosh", "arctanh",

        # Rounding
        "around", "round_", "rint", "fix", "floor", "ceil", "trunc",

        # Sums, products, differences

        # FIXME: Many of These are reductions or scans.
        # "prod", "sum", "nanprod", "nansum", "cumprod", "cumsum", "nancumprod",
        # "nancumsum", "diff", "ediff1d", "gradient", "cross", "trapz",

        # Exponents and logarithms
        "exp", "expm1", "exp2", "log", "log10", "log2", "log1p", "logaddexp",
        "logaddexp2",

        # Other special functions
        "i0", "sinc",

        # Floating point routines
        "signbit", "copysign", "frexp", "ldexp", "nextafter", "spacing",
        # Rational routines
        "lcm", "gcd",

        # Arithmetic operations
        "add", "reciprocal", "positive", "negative", "multiply", "divide", "power",
        "subtract", "true_divide", "floor_divide", "float_power", "fmod", "mod",
        "modf", "remainder", "divmod",

        # Handling complex numbers
        "angle", "real", "imag",
        # Implemented below:
        # "conj", "conjugate",

        # Miscellaneous
        "convolve", "clip", "sqrt", "cbrt", "square", "absolute", "abs", "fabs",
        "sign", "heaviside", "maximum", "fmax", "nan_to_num",

        # FIXME:
        # "interp",

        })

    _numpy_to_c_arc_functions = {
            "arcsin": "asin",
            "arccos": "acos",
            "arctan": "atan",
            "arctan2": "atan2",

            "arcsinh": "asinh",
            "arccosh": "acosh",
            "arctanh": "atanh",
            }

    _c_to_numpy_arc_functions = {c_name: numpy_name
            for numpy_name, c_name in _numpy_to_c_arc_functions.items()}

    def __getattr__(self, name):
        def loopy_implemented_elwise_func(*args):
            actx = self._array_context
            # FIXME: Maybe involve loopy type inference?
            prg = actx._get_scalar_func_loopy_program(
                    c_name, nargs=len(args), naxes=len(args[0].shape))
            result = actx.call_loopy(prg,
                    **{"inp%d" % i: arg for i, arg in enumerate(args)},
                    **{"n%d" % i: axis_len
                        for i, axis_len in enumerate(args[0].shape)},
                    )["out"]
            return result

        if name in self._c_to_numpy_arc_functions:
            from warnings import warn
            warn(f"'{name}' in ArrayContext.np is deprecated. "
                    "Use '{c_to_numpy_arc_functions[name]}' as in numpy. "
                    "The old name will stop working in 2021.",
                    DeprecationWarning, stacklevel=3)

        # normalize to C names anyway
        c_name = self._numpy_to_c_arc_functions.get(name, name)

        # limit which functions we try to hand off to loopy
        if name in self._numpy_math_functions:
            from meshmode.dof_array import obj_or_dof_array_vectorized_n_args
            return obj_or_dof_array_vectorized_n_args(loopy_implemented_elwise_func)
        else:
            raise AttributeError(name)

    def conjugate(self, x):
        # NOTE: conjugate distributes over object arrays, but it looks for a
        # `conjugate` ufunc, while some implementations only have the shorter
        # `conj` (e.g. cl.array.Array), so this should work for everybody.
        from meshmode.dof_array import obj_or_dof_array_vectorize
        return obj_or_dof_array_vectorize(lambda obj: obj.conj(), x)

    conj = conjugate


class _BaseFakeNumpyLinalgNamespace:
    def __init__(self, array_context):
        self._array_context = array_context


class ArrayContext:
    """An interface that allows a
    :class:`~meshmode.discretization.Discretization` to create and interact
    with arrays of degrees of freedom without fully specifying their types.

    .. automethod:: empty
    .. automethod:: zeros
    .. automethod:: empty_like
    .. automethod:: zeros_like
    .. automethod:: from_numpy
    .. automethod:: to_numpy
    .. automethod:: call_loopy
    .. attribute:: np

         Provides access to a namespace that serves as a work-alike to
         :mod:`numpy`.  The actual level of functionality provided is up to the
         individual array context implementation, however the functions and
         objects available under this namespace must not behave differently
         from :mod:`numpy`.

         As a baseline, special functions available through :mod:`loopy`
         (e.g. ``sin``, ``exp``) are accessible through this interface.

         Callables accessible through this namespace vectorize over object
         arrays, including :class:`meshmode.dof_array.DOFArray`.

    .. automethod:: freeze
    .. automethod:: thaw

    .. versionadded:: 2020.2
    """

    def __init__(self):
        self.np = self._get_fake_numpy_namespace()

    def _get_fake_numpy_namespace(self):
        return _BaseFakeNumpyNamespace(self)

    def empty(self, shape, dtype):
        raise NotImplementedError

    def zeros(self, shape, dtype):
        raise NotImplementedError

    def empty_like(self, ary):
        return self.empty(shape=ary.shape, dtype=ary.dtype)

    def zeros_like(self, ary):
        return self.zeros(shape=ary.shape, dtype=ary.dtype)

    def from_numpy(self, array: np.ndarray):
        r"""
        :returns: the :class:`numpy.ndarray` *array* converted to the
            array context's array type. The returned array will be
            :meth:`thaw`\ ed.
        """
        raise NotImplementedError

    def to_numpy(self, array):
        r"""
        :returns: *array*, an array recognized by the context, converted
            to a :class:`numpy.ndarray`. *array* must be
            :meth:`thaw`\ ed.
        """
        raise NotImplementedError

    def call_loopy(self, program, **kwargs):
        """Execute the :mod:`loopy` program *program* on the arguments
        *kwargs*.

        *program* is a :class:`loopy.LoopKernel` or :class:`loopy.LoopKernel`.
        It is expected to not yet be transformed for execution speed.
        It must have :attr:`loopy.Options.return_dict` set.

        :return: a :class:`dict` of outputs from the program, each an
            array understood by the context.
        """
        raise NotImplementedError

    @memoize_method
    def _get_scalar_func_loopy_program(self, c_name, nargs, naxes):
        from pymbolic import var

        var_names = ["i%d" % i for i in range(naxes)]
        size_names = ["n%d" % i for i in range(naxes)]
        subscript = tuple(var(vname) for vname in var_names)
        from islpy import make_zero_and_vars
        v = make_zero_and_vars(var_names, params=size_names)
        domain = v[0].domain()
        for vname, sname in zip(var_names, size_names):
            domain = domain & v[0].le_set(v[vname]) & v[vname].lt_set(v[sname])

        domain_bset, = domain.get_basic_sets()

        return make_loopy_program(
                [domain_bset],
                [
                    lp.Assignment(
                        var("out")[subscript],
                        var(c_name)(*[
                            var("inp%d" % i)[subscript] for i in range(nargs)]))
                    ],
                name="actx_special_%s" % c_name)

    def freeze(self, array):
        """Return a version of the context-defined array *array* that is
        'frozen', i.e. suitable for long-term storage and reuse. Frozen arrays
        do not support arithmetic. For example, in the context of
        :class:`~pyopencl.array.Array`, this might mean stripping the array
        of an associated command queue, whereas in a lazily-evaluated context,
        it might mean that the array is evaluated and stored.

        Freezing makes the array independent of this :class:`ArrayContext`;
        it is permitted to :meth:`thaw` it in a different one, as long as that
        context understands the array format.
        """
        raise NotImplementedError

    def thaw(self, array):
        """Take a 'frozen' array and return a new array representing the data in
        *array* that is able to perform arithmetic and other operations, using
        the execution resources of this context. In the context of
        :class:`~pyopencl.array.Array`, this might mean that the array is
        equipped with a command queue, whereas in a lazily-evaluated context,
        it might mean that the returned array is a symbol bound to
        the data in *array*.

        The returned array may not be used with other contexts while thawed.
        """
        raise NotImplementedError

    def compile(self, f: Callable[[Any], Any],
            input_like: np.array) -> Callable[[Any], Any]:
        """
        Returns a potentially more efficient implementation of the callable *f*.
        *f* is a side-effect free function that accepts a numpy object array
        of :class:`meshmode.dof_array.DOFArray`s shaped as *input_like* and returns
        a numpy object array of :class:`meshmode.dof_aray.DOFArray`s shaped as
        *output_like*.

        :arg output_like: if output_like is *None*. Then defaulted to *input_like*.
        """
        raise NotImplementedError

# }}}


# {{{ PyOpenCLArrayContext

class _PyOpenCLFakeNumpyNamespace(_BaseFakeNumpyNamespace):
    def _get_fake_numpy_linalg_namespace(self):
        return _PyOpenCLFakeNumpyLinalgNamespace(self._array_context)

    def maximum(self, x, y):
        import pyopencl.array as cl_array
        from meshmode.dof_array import obj_or_dof_array_vectorize_n_args
        return obj_or_dof_array_vectorize_n_args(
                partial(cl_array.maximum, queue=self._array_context.queue),
                x, y)

    def minimum(self, x, y):
        import pyopencl.array as cl_array
        from meshmode.dof_array import obj_or_dof_array_vectorize_n_args
        return obj_or_dof_array_vectorize_n_args(
                partial(cl_array.minimum, queue=self._array_context.queue),
                x, y)

    def where(self, criterion, then, else_):
        import pyopencl.array as cl_array
        from meshmode.dof_array import obj_or_dof_array_vectorize_n_args

        def where_inner(inner_crit, inner_then, inner_else):
            return cl_array.if_positive(inner_crit != 0, inner_then, inner_else,
                    queue=self._array_context.queue)

        return obj_or_dof_array_vectorize_n_args(where_inner, criterion, then, else_)

    def sum(self, a, dtype=None):
        import pyopencl.array as cl_array
        return cl_array.sum(
                a, dtype=dtype, queue=self._array_context.queue).get()[()]

    def min(self, a):
        import pyopencl.array as cl_array
        return cl_array.min(a, queue=self._array_context.queue).get()[()]

    def max(self, a):
        import pyopencl.array as cl_array
        return cl_array.max(a, queue=self._array_context.queue).get()[()]

    def reshape(self, a, newshape):
        import pyopencl.array as cl_array
        return cl_array.reshape(a, newshape)

    def concatenate(self, arrays, axis=0):
        import pyopencl.array as cl_array
        return cl_array.concatenate(arrays, axis,
                self._array_context.queue, self._array_context.allocator)

    def transpose(self, a, axes=None):
        import pyopencl.array as cl_array
        return cl_array.transpose(a, axes)


def _flatten_grp_array(grp_ary):
    if grp_ary.size == 0:
        # Work around https://github.com/inducer/pyopencl/pull/402
        return grp_ary._new_with_changes(
                data=None, offset=0, shape=(0,), strides=(grp_ary.dtype.itemsize,))
    if grp_ary.flags.f_contiguous:
        return grp_ary.reshape(-1, order="F")
    elif grp_ary.flags.c_contiguous:
        return grp_ary.reshape(-1, order="C")
    else:
        raise ValueError("cannot flatten group array of DOFArray for norm, "
                f"with strides {grp_ary.strides} of {grp_ary.dtype}")


class _PyOpenCLFakeNumpyLinalgNamespace(_BaseFakeNumpyLinalgNamespace):
    def norm(self, array, ord=None):
        if len(array.shape) != 1:
            raise NotImplementedError("only vector norms are implemented")

        if ord is None:
            ord = 2

        # Handling DOFArrays here is not beautiful, but it sure does avoid
        # downstream headaches.
        from meshmode.dof_array import DOFArray
        if isinstance(array, DOFArray):
            import numpy.linalg as la
            return la.norm(np.array([
                self.norm(_flatten_grp_array(grp_ary), ord)
                for grp_ary in array]), ord)

        if array.size == 0:
            return 0

        from numbers import Number
        if ord == np.inf:
            return self._array_context.np.max(abs(array))
        elif isinstance(ord, Number) and ord > 0:
            return self._array_context.np.sum(abs(array)**ord)**(1/ord)
        else:
            raise NotImplementedError(f"unsupported value of 'ord': {ord}")


class PyOpenCLArrayContext(ArrayContext):
    """
    A :class:`ArrayContext` that uses :class:`pyopencl.array.Array` instances
    for DOF arrays.

    .. attribute:: context

        A :class:`pyopencl.Context`.

    .. attribute:: queue

        A :class:`pyopencl.CommandQueue`.

    .. attribute:: allocator

        A PyOpenCL memory allocator. Can also be `None` (default) or `False` to
        use the default allocator. Please note that running with the default
        allocator allocates and deallocates OpenCL buffers directly. If lots
        of arrays are created (e.g. as results of computation), the associated cost
        may become significant. Using e.g. :class:`pyopencl.tools.MemoryPool`
        as the allocator can help avoid this cost.
    """

    def __init__(self, queue, allocator=None, wait_event_queue_length=None):
        r"""
        :arg wait_event_queue_length: The length of a queue of
            :class:`~pyopencl.Event` objects that are maintained by the
            array context, on a per-kernel-name basis. The events returned
            from kernel execution are appended to the queue, and Once the
            length of the queue exceeds *wait_event_queue_length*, the
            first event in the queue :meth:`pyopencl.Event.wait`\ ed on.

            *wait_event_queue_length* may be set to *False* to disable this feature.

            The use of *wait_event_queue_length* helps avoid enqueuing
            large amounts of work (and, potentially, allocating large amounts
            of memory) far ahead of the actual OpenCL execution front,
            by limiting the number of each type (name, really) of kernel
            that may reside unexecuted in the queue at one time.

        .. note::

            For now, *wait_event_queue_length* should be regarded as an
            experimental feature that may change or disappear at any minute.
        """
        super().__init__()
        self.context = queue.context
        self.queue = queue
        self.allocator = allocator if allocator else None

        if wait_event_queue_length is None:
            wait_event_queue_length = 10

        self._wait_event_queue_length = wait_event_queue_length
        self._kernel_name_to_wait_event_queue = {}

        import pyopencl as cl
        if allocator is None and queue.device.type & cl.device_type.GPU:
            from warnings import warn
            warn("PyOpenCLArrayContext created without an allocator on a GPU. "
                 "This can lead to high numbers of memory allocations. "
                 "Please consider using a pyopencl.tools.MemoryPool. "
                 "Run with allocator=False to disable this warning.")

    def _get_fake_numpy_namespace(self):
        return _PyOpenCLFakeNumpyNamespace(self)

    # {{{ ArrayContext interface

    def empty(self, shape, dtype):
        import pyopencl.array as cla
        return cla.empty(self.queue, shape=shape, dtype=dtype,
                allocator=self.allocator)

    def zeros(self, shape, dtype):
        import pyopencl.array as cla
        return cla.zeros(self.queue, shape=shape, dtype=dtype,
                allocator=self.allocator)

    def from_numpy(self, np_array: np.ndarray):
        import pyopencl.array as cla
        return cla.to_device(self.queue, np_array, allocator=self.allocator)

    def to_numpy(self, array):
        return array.get(queue=self.queue)

    def call_loopy(self, program, **kwargs):
        program = self.transform_loopy_program(program)

        evt, result = program(self.queue, **kwargs, allocator=self.allocator)

        if self._wait_event_queue_length is not False:
            try:
                name = program.name
            except AttributeError:
                name, = program.entrypoints
            wait_event_queue = self._kernel_name_to_wait_event_queue.setdefault(
                    name, [])

            wait_event_queue.append(evt)
            if len(wait_event_queue) > self._wait_event_queue_length:
                wait_event_queue.pop(0).wait()

        return result

    def freeze(self, array):
        array.finish()
        return array.with_queue(None)

    def thaw(self, array):
        return array.with_queue(self.queue)

    # }}}

    @memoize_method
    def transform_loopy_program(self, program):
        # accommodate loopy with and without kernel callables
        try:
            options = program.options
        except AttributeError:
            try:
                options = program.root_kernel.options
            except AttributeError:
                entrypoint, = program.entrypoints
                options = program[entrypoint].options
        if not (options.return_dict and options.no_numpy):
            raise ValueError("Loopy program passed to call_loopy must "
                    "have return_dict and no_numpy options set. "
                    "Did you use meshmode.array_context.make_loopy_program "
                    "to create this program?")

        # FIXME: This could be much smarter.
        import loopy as lp
        # accommodate loopy with and without kernel callables
        try:
            all_inames = program.all_inames()
        except AttributeError:
            try:
                all_inames = program.root_kernel.all_inames()
            except AttributeError:
                entrypoint, = program.entrypoints
                all_inames = program[entrypoint].all_inames()

        inner_iname = None
        if "iel" not in all_inames and "i0" in all_inames:
            outer_iname = "i0"

            if "i1" in all_inames:
                inner_iname = "i1"
        elif "iel" in all_inames:
            outer_iname = "iel"

            if "idof" in all_inames:
                inner_iname = "idof"
        else:
            # cannot "fit" the optimization strategy for the provided kernel
            # => bail
            return program

        if inner_iname is not None:
            program = lp.split_iname(program, inner_iname, 16, inner_tag="l.0")
        return lp.tag_inames(program, {outer_iname: "g.0"})

    def compile(self, f: Callable[[Any], Any],
            input_like: np.array) -> Callable[[Any], Any]:
        return f

# }}}


# {{{ pytest integration

def pytest_generate_tests_for_pyopencl_array_context(metafunc):
    """Parametrize tests for pytest to use a :mod:`pyopencl` array context.

    Performs device enumeration analogously to
    :func:`pyopencl.tools.pytest_generate_tests_for_pyopencl`.

    Using the line:

    .. code-block:: python

       from meshmode.array_context import pytest_generate_tests_for_pyopencl \
            as pytest_generate_tests

    in your pytest test scripts allows you to use the arguments ctx_factory,
    device, or platform in your test functions, and they will automatically be
    run for each OpenCL device/platform in the system, as appropriate.

    It also allows you to specify the ``PYOPENCL_TEST`` environment variable
    for device selection.
    """

    import pyopencl as cl
    from pyopencl.tools import _ContextFactory

    class ArrayContextFactory(_ContextFactory):
        def __call__(self):
            ctx = super().__call__()
            return PyOpenCLArrayContext(cl.CommandQueue(ctx))

        def __str__(self):
            return ("<array context factory for <pyopencl.Device '%s' on '%s'>" %
                    (self.device.name.strip(),
                     self.device.platform.name.strip()))

    import pyopencl.tools as cl_tools
    arg_names = cl_tools.get_pyopencl_fixture_arg_names(
            metafunc, extra_arg_names=["actx_factory"])

    if not arg_names:
        return

    arg_values, ids = cl_tools.get_pyopencl_fixture_arg_values()
    if "actx_factory" in arg_names:
        if "ctx_factory" in arg_names or "ctx_getter" in arg_names:
            raise RuntimeError("Cannot use both an 'actx_factory' and a "
                    "'ctx_factory' / 'ctx_getter' as arguments.")

        for arg_dict in arg_values:
            arg_dict["actx_factory"] = ArrayContextFactory(arg_dict["device"])

    arg_values = [
            tuple(arg_dict[name] for name in arg_names)
            for arg_dict in arg_values
            ]

    metafunc.parametrize(arg_names, arg_values, ids=ids)

# }}}


# {{{ PytatoArrayContext

class _PytatoFakeNumpyLinalgNamespace(_BaseFakeNumpyLinalgNamespace):
    def norm(self, array, ord=None):
        raise NotImplementedError


class _PytatoFakeNumpyNamespace(_BaseFakeNumpyNamespace):
    def _get_fake_numpy_linalg_namespace(self):
        return _PytatoFakeNumpyLinalgNamespace(self._array_context)

    @property
    def ns(self):
        return self._array_context.ns

    def exp(self, x):
        import pytato as pt
        from meshmode.dof_array import obj_or_dof_array_vectorize
        return obj_or_dof_array_vectorize(pt.exp, x)

    def reshape(self, a, newshape):
        import pytato as pt

        from meshmode.dof_array import obj_or_dof_array_vectorize_n_args
        return obj_or_dof_array_vectorize_n_args(pt.reshape, a, newshape)

    def transpose(self, a, axes=None):
        import pytato as pt

        from meshmode.dof_array import obj_or_dof_array_vectorize_n_args
        return obj_or_dof_array_vectorize_n_args(pt.transpose, a, axes)

    def concatenate(self, arrays, axis=0):
        raise NotImplementedError
        import pytato as pt
        return pt.concatenate(arrays, axis)


class PytatoCompiledOperator:
    def __init__(self, actx, pytato_program, input_spec, output_spec):
        self.actx = actx
        self.pytato_program = pytato_program
        self.input_spec = input_spec
        self.output_spec = output_spec

    def __call__(self, fields):
        import pytato as pt
        import pyopencl.array as cla
        from meshmode.dof_array import DOFArray
        from pytools.obj_array import flat_obj_array

        def from_obj_array_to_input_dict(array):
            input_dict = {}
            for i in range(len(self.input_spec)):
                for j in range(self.input_spec[i]):
                    ary = array[i][j]
                    if isinstance(ary, pt.array.DataWrapper):
                        input_dict[f"_msh_inp_{i}_{j}"] = ary.data
                    elif isinstance(ary, cla.Array):
                        input_dict[f"_msh_inp_{i}_{j}"] = ary
                    else:
                        raise TypeError("Expect pt.DataWrapper or CL-array, got "
                                f"{type(ary)}")

            return input_dict

        def from_return_dict_to_obj_array(return_dict):
            return flat_obj_array([DOFArray.from_list(self.actx,
                [return_dict[f"_msh_out_{i}_{j}"]
                 for j in range(self.output_spec[i])])
                for i in range(len(self.output_spec))])

        in_dict = from_obj_array_to_input_dict(fields)
        evt, out_dict = self.pytato_program(allocator=self.actx.allocator, **in_dict)
        evt.wait()

        return from_return_dict_to_obj_array(out_dict)


class PytatoArrayContext(ArrayContext):
    """
    A :class:`ArrayContext` that uses :mod:`pytato` data types to represent
    the DOF arrays targeting OpenCL for offloading operations.

    .. attribute:: context

        A :class:`pyopencl.Context`.

    .. attribute:: queue

        A :class:`pyopencl.CommandQueue`.
    """
    def __init__(self, queue, allocator=None):
        import pytato as pt
        super().__init__()
        self.queue = queue
        self.allocator = allocator
        self.ns = pt.Namespace()
        self.np = self._get_fake_numpy_namespace()

    def _get_fake_numpy_namespace(self):
        return _PytatoFakeNumpyNamespace(self)

    # {{{ ArrayContext interface

    def empty(self, shape, dtype):
        raise ValueError("PytatoArrayContext does not support empty")

    def symbolic_array_var(self, shape, dtype, name=None):
        import pytato as pt
        return pt.make_placeholder(self.ns, shape=shape, dtype=dtype, name=name)

    def zeros(self, shape, dtype):
        import pytato as pt
        return pt.zeros(self.ns, shape, dtype)

    def from_numpy(self, np_array: np.ndarray):
        import pytato as pt
        import pyopencl.array as cla
        cl_array = cla.to_device(self.queue, np_array)
        return pt.make_data_wrapper(self.ns, cl_array)

    def to_numpy(self, array):
        cl_array = self.freeze(array)
        return cl_array.get(queue=self.queue)

    def call_loopy(self, program, **kwargs):
        from pytato.loopy import call_loopy
        import pyopencl.array as cla
        entrypoint, = set(program.callables_table)

        # thaw frozen arrays
        kwargs = {kw: (self.thaw(arg) if isinstance(arg, cla.Array) else arg)
                  for kw, arg in kwargs.items()}

        return call_loopy(self.ns, program, kwargs, entrypoint)

    def freeze(self, array):
        import pytato as pt
        import pyopencl.array as cla

        if isinstance(array, pt.Placeholder):
            raise ValueError("freezing placeholder would return garbage valued"
                    " arrays")
        if isinstance(array, cla.Array):
            return array.with_queue(None)
        if not isinstance(array, pt.Array):
            raise TypeError("PytatoArrayContext.freeze invoked with non-pt arrays")

        prg = pt.generate_loopy(array, target=pt.LoopyPyOpenCLTarget(self.queue))
        evt, (cl_array,) = prg()
        evt.wait()

        return cl_array.with_queue(None)

    def thaw(self, array):
        import pytato as pt
        import pyopencl.array as cla

        if not isinstance(array, cla.Array):
            raise TypeError("PytatoArrayContext.thaw expects CL arrays, got "
                    f"{type(array)}")

        return pt.make_data_wrapper(self.ns, array.with_queue(self.queue))

    # }}}

    def compile(self, f: Callable[[Any], Any],
            input_like: np.array) -> Callable[[Any], Any]:
        from pytools.obj_array import flat_obj_array
        from meshmode.dof_array import DOFArray
        import pytato as pt

        def make_placeholder_like(fields_obj_ary):
            return flat_obj_array([DOFArray.from_list(self,
                [pt.make_placeholder(self.ns, grp_ary.shape,
                                     grp_ary.dtype, f"_msh_inp_{i}_{j}")
                 for j, grp_ary in enumerate(dof_ary)])
                for i, dof_ary in enumerate(fields_obj_ary)])

        def as_dict_of_named_arrays(fields_obj_ary):
            dict_of_named_arrays = {}
            # output_spec: a list of length #fields; ith-entry denotes #groups in
            # ith-field
            output_spec = []
            for i, field in enumerate(fields_obj_ary):
                output_spec.append(len(field))
                for j, grp in enumerate(field):
                    dict_of_named_arrays[f"_msh_out_{i}_{j}"] = grp

            return pt.make_dict_of_named_arrays(dict_of_named_arrays), output_spec

        outputs = f(make_placeholder_like(input_like))
        output_dict_of_named_arrays, output_spec = as_dict_of_named_arrays(outputs)

        pytato_program = pt.generate_loopy(output_dict_of_named_arrays,
                          options={"return_dict": True},
                          target=pt.LoopyPyOpenCLTarget(self.queue))

        if False:
            from time import time
            start = time()
            # transforming leads to compile-time slow downs (turning off for now)
            pytato_program.program = self.transform_loopy_program(
                    pytato_program.program)
            end = time()
            print(f"Transforming took {end-start} secs")

        return PytatoCompiledOperator(self, pytato_program, [len(field) for field in
            input_like], output_spec)

    def transform_loopy_program(self, prg):
        from loopy.program import iterate_over_kernels_if_given_program

        nwg = 48
        nwi = (16, 2)

        @iterate_over_kernels_if_given_program
        def gridify(knl):
            # {{{ Pattern matching inames

            for insn in knl.instructions:
                if isinstance(insn, lp.CallInstruction):
                    # must be a callable kernel, don't touch.
                    pass
                elif isinstance(insn, lp.Assignment):
                    bigger_loop = None
                    smaller_loop = None
                    for iname in insn.within_inames:
                        if iname.startswith("iel"):
                            assert bigger_loop is None
                            bigger_loop = iname
                        if iname.startswith("idof"):
                            assert smaller_loop is None
                            smaller_loop = iname

                    if bigger_loop or smaller_loop:
                        assert bigger_loop is not None and smaller_loop is not None
                    else:
                        sorted_inames = sorted(tuple(insn.within_inames),
                                key=knl.get_constant_iname_length)
                        smaller_loop = sorted_inames[0]
                        bigger_loop = sorted_inames[1]

                    knl = lp.chunk_iname(knl, bigger_loop, nwg,
                            outer_tag="g.0")
                    knl = lp.split_iname(knl, f"{bigger_loop}_inner",
                            nwi[0], inner_tag="l.1")
                    knl = lp.split_iname(knl, smaller_loop,
                            nwi[1], inner_tag="l.0")
                elif isinstance(insn, lp.BarrierInstruction):
                    pass
                else:
                    raise NotImplementedError

            # }}}

            return knl

        prg = lp.set_options(prg, "insert_additional_gbarriers")

        return gridify(prg)


# }}}

# vim: foldmethod=marker
