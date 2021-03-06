from collections import OrderedDict, defaultdict

from devito.ir.support.space import Interval
from devito.ir.support.stencil import Stencil
from devito.symbolics import retrieve_indexed, retrieve_terminals
from devito.tools import as_tuple, flatten, filter_sorted
from devito.types import Dimension, ModuloDimension

__all__ = ['detect_accesses', 'detect_oobs', 'build_iterators', 'build_intervals',
           'align_accesses', 'detect_io']


def detect_accesses(expr):
    """
    Return a mapper ``M : F -> S``, where F are Functions appearing
    in ``expr`` and S are Stencils. ``M[f]`` represents all data accesses
    to ``f`` within ``expr``. Also map ``M[None]`` to all Dimensions used in
    ``expr`` as plain symbols, rather than as array indices.
    """
    # Compute M : F -> S
    mapper = defaultdict(Stencil)
    for e in retrieve_indexed(expr, deep=True):
        f = e.function
        for a in e.indices:
            if isinstance(a, Dimension):
                mapper[f][a].update([0])
            d = None
            off = []
            for i in a.args:
                if isinstance(i, Dimension):
                    d = i
                elif i.is_integer:
                    off += [int(i)]
            if d is not None:
                mapper[f][d].update(off or [0])

    # Compute M[None]
    other_dims = [i for i in retrieve_terminals(expr) if isinstance(i, Dimension)]
    other_dims.extend(list(expr.implicit_dims))
    mapper[None] = Stencil([(i, 0) for i in other_dims])

    return mapper


def detect_oobs(mapper):
    """
    Given M as produced by :func:`detect_accesses`, return the set of
    Dimensions that *cannot* be iterated over the entire computational
    domain, to avoid out-of-bounds (OOB) accesses.
    """
    found = set()
    for f, stencil in mapper.items():
        if f is None or not f.is_DiscreteFunction:
            continue
        for d, v in stencil.items():
            p = d.parent if d.is_Sub else d
            try:
                test0 = min(v) < 0
                test1 = max(v) > f._size_nodomain[p].left + f._size_halo[p].right
                if test0 or test1:
                    # It'd mean trying to access a point before the
                    # left padding (test0) or after the right halo (test1)
                    found.add(p)
            except KeyError:
                # Unable to detect presence of OOB accesses
                # (/p/ not in /f._size_halo/, typical of indirect
                # accesses such as A[B[i]])
                pass
    return found | {i.parent for i in found if i.is_Derived}


def build_iterators(mapper):
    """
    Given M as produced by :func:`detect_accesses`, return a mapper ``M' : D -> V``,
    where D is the set of Dimensions in M, and V is a set of
    DerivedDimensions. M'[d] provides the sub-iterators along the
    Dimension `d`.
    """
    iterators = OrderedDict()
    for k, v in mapper.items():
        for d, offs in v.items():
            if d.is_Stepping:
                sub_iterators = iterators.setdefault(d.parent, set())
                sub_iterators.update({ModuloDimension(d, i, k._time_size)
                                      for i in offs})
            elif d.is_Conditional:
                # There are no iterators associated to a ConditionalDimension
                continue
            else:
                iterators.setdefault(d, set())
    return {k: tuple(v) for k, v in iterators.items()}


def build_intervals(stencil):
    """
    Given a Stencil, return an iterable of Intervals, one
    for each Dimension in the stencil.
    """
    mapper = {}
    for d, offs in stencil.items():
        dim = d.parent if d.is_NonlinearDerived else d
        mapper.setdefault(dim, set()).update(offs)
    return [Interval(d, min(offs), max(offs)) for d, offs in mapper.items()]


def align_accesses(expr, key=lambda i: False):
    """
    ``expr -> expr'``, with ``expr'`` semantically equivalent to ``expr``, but
    with data accesses aligned to the domain if ``key(function)`` gives True.
    """
    mapper = {}
    for indexed in retrieve_indexed(expr):
        f = indexed.function
        if not key(f):
            continue
        subs = {i: i + j for i, j in zip(indexed.indices, f._size_nodomain.left)}
        mapper[indexed] = indexed.xreplace(subs)
    return expr.xreplace(mapper)


def detect_io(exprs, relax=False):
    """
    ``{exprs} -> ({reads}, {writes})``

    Parameters
    ----------
    exprs : expr-like or list of expr-like
        The searched expressions.
    relax : bool, optional
        If False, as by default, collect only Constants and Functions.
        Otherwise, collect any Basic object.
    """
    exprs = as_tuple(exprs)
    if relax is False:
        rule = lambda i: i.is_Input
    else:
        rule = lambda i: i.is_Scalar or i.is_Tensor

    # Don't forget this nasty case, with indirections on the LHS:
    # >>> u[t, a[x]] = f[x]  -> (reads={a, f}, writes={u})

    roots = []
    for i in exprs:
        try:
            roots.append(i.rhs)
            roots.extend(list(i.lhs.indices))
        except AttributeError:
            # E.g., FunctionFromPointer
            roots.append(i)

    reads = []
    terminals = flatten(retrieve_terminals(i, deep=True) for i in roots)
    for i in terminals:
        candidates = i.free_symbols
        try:
            candidates.update({i.function})
        except AttributeError:
            pass
        for j in candidates:
            try:
                if rule(j):
                    reads.append(j)
            except AttributeError:
                pass

    writes = []
    for i in exprs:
        try:
            f = i.lhs.function
        except AttributeError:
            continue
        if rule(f):
            writes.append(f)

    return filter_sorted(reads), filter_sorted(writes)
