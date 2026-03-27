"""
Boolean expression tree with constant propagation and simplification.

Node types:
  Const(0 | 1)   — constant
  Lit(name, neg)  — literal or its complement
  Not(arg)        — inversion
  And(*args)      — conjunction
  Or(*args)       — disjunction
"""

from __future__ import annotations
from typing import Dict, Set


# ═══════════════════════════════════════════════════════════════════════════════
#  Base
# ═══════════════════════════════════════════════════════════════════════════════

class Expr:
    """Abstract base for boolean expression nodes."""
    def eval(self, asgn: Dict[str, int]) -> int:    raise NotImplementedError
    def literals(self) -> int:                       raise NotImplementedError
    def vars(self) -> Set[str]:                      raise NotImplementedError
    def sub(self, var: str, val: int) -> 'Expr':     raise NotImplementedError
    def __str__(self) -> str:                        raise NotImplementedError
    def __repr__(self) -> str:                       return str(self)


# ═══════════════════════════════════════════════════════════════════════════════
#  Leaf nodes
# ═══════════════════════════════════════════════════════════════════════════════

class Const(Expr):
    __slots__ = ('v',)

    def __init__(self, v: int):
        self.v = v

    def eval(self, _):      return self.v
    def literals(self):     return 0
    def vars(self):         return set()
    def sub(self, *_):      return self
    def __str__(self):      return str(self.v)
    def __eq__(self, o):    return isinstance(o, Const) and self.v == o.v
    def __hash__(self):     return hash(('Const', self.v))


ONE  = Const(1)
ZERO = Const(0)


class Lit(Expr):
    """A single literal: variable or its complement."""
    __slots__ = ('name', 'neg')

    def __init__(self, name: str, neg: bool = False):
        self.name = name
        self.neg  = neg

    def eval(self, a: Dict[str, int]) -> int:
        v = a.get(self.name, 0)
        return 1 - v if self.neg else v

    def literals(self):  return 1
    def vars(self):      return {self.name}

    def sub(self, var: str, val: int) -> Expr:
        if self.name != var:
            return self
        v = 1 - val if self.neg else val
        return ONE if v else ZERO

    def __str__(self):
        return f'~{self.name}' if self.neg else self.name

    def __eq__(self, o):
        return isinstance(o, Lit) and self.name == o.name and self.neg == o.neg

    def __hash__(self):
        return hash(('Lit', self.name, self.neg))


# ═══════════════════════════════════════════════════════════════════════════════
#  Composite nodes
# ═══════════════════════════════════════════════════════════════════════════════

class Not(Expr):
    __slots__ = ('arg',)

    def __init__(self, arg: Expr):
        self.arg = arg

    def eval(self, a):      return 1 - self.arg.eval(a)
    def literals(self):     return self.arg.literals()
    def vars(self):         return self.arg.vars()
    def sub(self, var, val): return simp(Not(self.arg.sub(var, val)))

    def __str__(self):
        inner = str(self.arg)
        return f'~({inner})' if not isinstance(self.arg, (Lit, Const)) else f'~{inner}'

    def __eq__(self, o):    return isinstance(o, Not) and self.arg == o.arg
    def __hash__(self):     return hash(('Not', self.arg))


class And(Expr):
    __slots__ = ('args',)

    def __init__(self, *args: Expr):
        self.args = list(args)

    def eval(self, a):      return int(all(c.eval(a) for c in self.args))
    def literals(self):     return sum(c.literals() for c in self.args)
    def vars(self):         return set().union(*(c.vars() for c in self.args))
    def sub(self, var, val): return simp(And(*[c.sub(var, val) for c in self.args]))

    def __str__(self):
        parts = [f'({c})' if isinstance(c, Or) else str(c) for c in self.args]
        return ' & '.join(parts)

    def __eq__(self, o):    return isinstance(o, And) and self.args == o.args
    def __hash__(self):     return hash(('And', tuple(self.args)))


class Or(Expr):
    __slots__ = ('args',)

    def __init__(self, *args: Expr):
        self.args = list(args)

    def eval(self, a):      return int(any(c.eval(a) for c in self.args))
    def literals(self):     return sum(c.literals() for c in self.args)
    def vars(self):         return set().union(*(c.vars() for c in self.args))
    def sub(self, var, val): return simp(Or(*[c.sub(var, val) for c in self.args]))

    def __str__(self):
        parts = [f'({c})' if isinstance(c, And) else str(c) for c in self.args]
        return ' | '.join(parts)

    def __eq__(self, o):    return isinstance(o, Or) and self.args == o.args
    def __hash__(self):     return hash(('Or', tuple(self.args)))


# ═══════════════════════════════════════════════════════════════════════════════
#  Simplifier
# ═══════════════════════════════════════════════════════════════════════════════

def simp(e: Expr) -> Expr:
    """Constant-propagation + double-negation elimination."""
    if isinstance(e, And):
        args = []
        for c in e.args:
            c = simp(c)
            if c == ZERO: return ZERO
            if c != ONE:  args.append(c)
        if not args:       return ONE
        if len(args) == 1: return args[0]
        return And(*args)

    if isinstance(e, Or):
        args = []
        for c in e.args:
            c = simp(c)
            if c == ONE:  return ONE
            if c != ZERO: args.append(c)
        if not args:       return ZERO
        if len(args) == 1: return args[0]
        return Or(*args)

    if isinstance(e, Not):
        a = simp(e.arg)
        if a == ONE:            return ZERO
        if a == ZERO:           return ONE
        if isinstance(a, Not):  return simp(a.arg)
        return Not(a)

    return e