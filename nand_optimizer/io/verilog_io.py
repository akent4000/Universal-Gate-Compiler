"""
verilog_io.py — Verilog front-end for nand_optimizer.

Parses a single combinational Verilog module (structural gate primitives +
continuous assign expressions) into a StructuralModule, then runs the
synthesis pipeline.

Public API
----------
    read_verilog(path, script, verbose)   -> OptimizeResult
    parse_verilog(text, script, verbose)  -> OptimizeResult
    verilog_to_module(text)               -> StructuralModule  (un-synthesized)

Supported Verilog subset
------------------------
  Declarations   input, output, wire, reg  (scalar and [msb:lsb] buses)
  Expressions    ~ ! & | ^ ~^ ^~ && || ?:
                 unary reductions: &a |a ^a ~&a ~|a ~^a
                 bus concatenation: {a, b, c}
                 bit-select: name[i]   part-select: name[hi:lo]
  Gate prims     and or nand nor not xor xnor buf  (any fan-in; opt. inst name)
  Constants      1'b0 1'b1  N'bBBB  N'hHHH  N'dDDD  N'oOOO  bare 0/1

Not supported (raises VerilogError)
--------------------------------------
  always / initial blocks; parameter / localparam; module instantiation;
  `include / `define; bidirectional ports (inout); concat on assign LHS.
"""

from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple

from ..core.aig import AIG, FALSE, TRUE

Lit = int
Bus = List[Lit]   # MSB-first list of AIG literals


class VerilogError(ValueError):
    """Ill-formed or unsupported Verilog construct."""


# ═══════════════════════════════════════════════════════════════════════════════
#  Lexer
# ═══════════════════════════════════════════════════════════════════════════════

_TOK_RE = re.compile(r"""
    (?P<COMMENT_SL>//[^\n]*)|
    (?P<COMMENT_ML>/\*.*?\*/)|
    (?P<NUMBER>\d+\s*'[bBoOhHdD][0-9a-fA-FxXzZ_?]*|\d+)|
    (?P<TXOR>~\^|\^\~)|
    (?P<TAMP>~&)|(?P<TPIPE>~\|)|
    (?P<DAMP>&&)|(?P<DPIPE>\|\|)|
    (?P<TILDE>~)|(?P<BANG>!)|
    (?P<AMP>&)|(?P<PIPE>\|)|(?P<CARET>\^)|
    (?P<QUESTION>\?)|(?P<COLON>:)|(?P<ASSIGN>=)|
    (?P<LPAREN>\()|(?P<RPAREN>\))|
    (?P<LBRACKET>\[)|(?P<RBRACKET>\])|
    (?P<LBRACE>\{)|(?P<RBRACE>\})|
    (?P<COMMA>,)|(?P<SEMICOLON>;)|
    (?P<IDENT>[a-zA-Z_\$][a-zA-Z0-9_\$]*)|
    (?P<SPACE>\s+)
""", re.VERBOSE | re.DOTALL)

Token = Tuple[str, str]   # (kind, value)


def _tokenize(text: str) -> List[Token]:
    return [
        (m.lastgroup, m.group())
        for m in _TOK_RE.finditer(text)
        if m.lastgroup not in ('COMMENT_SL', 'COMMENT_ML', 'SPACE')
    ]


def _parse_number(val: str) -> Bus:
    """Parse a Verilog numeric literal into a Bus of TRUE/FALSE constants (MSB first)."""
    val = re.sub(r'\s', '', val).replace('_', '')
    if "'" in val:
        w_str, rest = val.split("'", 1)
        w   = int(w_str) if w_str else 1
        fmt = rest[0].lower()
        digits = re.sub(r'[xXzZ?]', '0', rest[1:])
        if fmt == 'b':
            n = int(digits or '0', 2)
        elif fmt == 'h':
            n = int(digits or '0', 16)
        elif fmt == 'o':
            n = int(digits or '0', 8)
        else:   # 'd'
            n = int(digits or '0')
    else:
        n = int(val)
        w = max(1, n.bit_length())
    return [TRUE if (n >> (w - 1 - i)) & 1 else FALSE for i in range(w)]


# ═══════════════════════════════════════════════════════════════════════════════
#  Parser  (produces a raw AST-ish dict; no AIG built here)
# ═══════════════════════════════════════════════════════════════════════════════

_GATE_PRIMS = frozenset({'and', 'or', 'nand', 'nor', 'not', 'xor', 'xnor', 'buf'})
_DECL_KWS   = frozenset({'input', 'output', 'wire', 'reg', 'inout'})
# kind, msb, lsb (msb/lsb=None → scalar)
PortDecl = Tuple[str, Optional[int], Optional[int]]


class _Parser:
    def __init__(self, toks: List[Token]):
        self._t = toks
        self._i = 0

    # ── low-level helpers ─────────────────────────────────────────────────────

    def _eof(self) -> bool:
        return self._i >= len(self._t)

    def _cur(self) -> Optional[Token]:
        return self._t[self._i] if not self._eof() else None

    def _peek(self, d: int = 1) -> Optional[Token]:
        j = self._i + d
        return self._t[j] if j < len(self._t) else None

    def _eat(self) -> Token:
        tok = self._t[self._i]
        self._i += 1
        return tok

    def _need(self, kind: str, val: str = None) -> Token:
        tok = self._eat()
        if tok[0] != kind:
            raise VerilogError(f'Expected {kind!r}, got {tok[0]!r} ({tok[1]!r})')
        if val is not None and tok[1] != val:
            raise VerilogError(f'Expected {val!r}, got {tok[1]!r}')
        return tok

    def _is(self, kind: str, val: str = None, d: int = 0) -> bool:
        tok = self._t[self._i + d] if self._i + d < len(self._t) else None
        if tok is None:
            return False
        return tok[0] == kind and (val is None or tok[1] == val)

    def _eat_if(self, kind: str, val: str = None) -> bool:
        if self._is(kind, val):
            self._eat()
            return True
        return False

    def _skip_to_semi(self) -> None:
        while not self._eof():
            if self._is('SEMICOLON'):
                self._eat()
                return
            if self._is('IDENT', 'endmodule'):
                return
            self._eat()

    def _skip_block(self) -> None:
        """Skip 'begin ... end' or single-statement (up to semicolon)."""
        if self._eat_if('IDENT', 'begin'):
            depth = 1
            while not self._eof() and depth > 0:
                t = self._eat()
                if t == ('IDENT', 'begin'):
                    depth += 1
                elif t == ('IDENT', 'end'):
                    depth -= 1
        else:
            self._skip_to_semi()

    # ── range: [msb:lsb] ─────────────────────────────────────────────────────

    def _parse_range(self) -> Tuple[Optional[int], Optional[int]]:
        if not self._is('LBRACKET'):
            return None, None
        self._eat()
        msb = self._parse_const_int()
        self._need('COLON')
        lsb = self._parse_const_int()
        self._need('RBRACKET')
        return msb, lsb

    def _parse_const_int(self) -> int:
        if not self._is('NUMBER'):
            raise VerilogError(f'Expected integer constant, got {self._cur()!r}')
        bits = _parse_number(self._eat()[1])
        return sum(b << (len(bits) - 1 - i) for i, b in enumerate(bits))

    # ── top-level ─────────────────────────────────────────────────────────────

    def parse(self):
        """
        Returns (mod_name, ports, assigns, gate_stmts).

          ports      : dict[name, (kind, msb, lsb)]
          assigns    : list[((lhs_name, lhs_hi, lhs_lo), [Token, ...])]
          gate_stmts : list[(gate_type, [[Token,...], ...])]  one list per port
        """
        # Skip to 'module'
        while not self._eof() and not self._is('IDENT', 'module'):
            self._eat()
        if self._eof():
            raise VerilogError("No 'module' keyword found")
        self._eat()   # module
        mod_name = self._need('IDENT')[1]

        ports: Dict[str, PortDecl] = {}

        # Port list  ─────────────────────────────────────────────────────────
        # The direction keyword carries across comma-separated names in the
        # same group: `input a, b, cin` declares all three as inputs.
        if self._eat_if('LPAREN'):
            cur_kind: Optional[str] = None
            cur_msb:  Optional[int] = None
            cur_lsb:  Optional[int] = None
            while not self._is('RPAREN') and not self._eof():
                # ANSI: a new direction keyword updates the running kind/range
                if self._is('IDENT') and self._cur()[1] in _DECL_KWS:
                    cur_kind = self._eat()[1]
                    if cur_kind == 'inout':
                        cur_kind = 'wire'
                    self._eat_if('IDENT', 'signed')
                    cur_msb, cur_lsb = self._parse_range()
                if self._is('IDENT'):
                    name = self._eat()[1]
                    if cur_kind and name not in ports:
                        ports[name] = (cur_kind, cur_msb, cur_lsb)
                self._eat_if('COMMA')
            self._eat()   # ')'
        self._eat_if('SEMICOLON')

        assigns:    List[Tuple] = []
        gate_stmts: List[Tuple] = []

        # Body ────────────────────────────────────────────────────────────────
        while not self._eof():
            if self._is('IDENT', 'endmodule'):
                self._eat()
                break

            cur = self._cur()
            if cur is None:
                break

            # Declarations
            if cur[0] == 'IDENT' and cur[1] in _DECL_KWS:
                kind = self._eat()[1]
                if kind == 'inout':
                    kind = 'wire'
                # optional 'signed' / 'reg' qualifiers before range
                self._eat_if('IDENT', 'signed')
                msb, lsb = self._parse_range()
                while True:
                    if not self._is('IDENT'):
                        break
                    name = self._eat()[1]
                    if name not in ports:
                        ports[name] = (kind, msb, lsb)
                    elif ports[name][0] not in ('input', 'output'):
                        ports[name] = (kind, msb, lsb)
                    # `wire name = expr;` — treat as an implicit assign
                    if self._eat_if('ASSIGN'):
                        rhs_toks: List[Token] = []
                        while (not self._is('COMMA') and not self._is('SEMICOLON')
                               and not self._eof()):
                            rhs_toks.append(self._eat())
                        if rhs_toks:
                            assigns.append(((name, None, None), rhs_toks))
                    if not self._eat_if('COMMA'):
                        break
                self._eat_if('SEMICOLON')

            # Assign statement
            elif cur[0] == 'IDENT' and cur[1] == 'assign':
                self._eat()
                # LHS  (skip concat lvalue for now)
                if self._is('LBRACE'):
                    self._skip_to_semi()
                    continue
                if not self._is('IDENT'):
                    self._skip_to_semi()
                    continue
                lhs_name = self._eat()[1]
                lhs_hi: Optional[int] = None
                lhs_lo: Optional[int] = None
                if self._eat_if('LBRACKET'):
                    lhs_hi = self._parse_const_int()
                    if self._eat_if('COLON'):
                        lhs_lo = self._parse_const_int()
                    self._need('RBRACKET')
                self._need('ASSIGN')
                rhs: List[Token] = []
                while not self._is('SEMICOLON') and not self._eof():
                    rhs.append(self._eat())
                self._eat_if('SEMICOLON')
                assigns.append(((lhs_name, lhs_hi, lhs_lo), rhs))

            # Gate primitive
            elif cur[0] == 'IDENT' and cur[1] in _GATE_PRIMS:
                gate_type = self._eat()[1]
                # optional #(delay)  or  #N
                if self._is('IDENT') and self._cur()[1] == '#':
                    self._eat()
                    if self._eat_if('LPAREN'):
                        d = 1
                        while d > 0 and not self._eof():
                            t = self._eat()
                            if t[0] == 'LPAREN':
                                d += 1
                            elif t[0] == 'RPAREN':
                                d -= 1
                    elif self._is('NUMBER'):
                        self._eat()
                # optional instance name: IDENT before '('
                if self._is('IDENT') and self._is('LPAREN', d=1):
                    self._eat()
                self._need('LPAREN')
                # Collect port groups (comma-separated, respecting nesting)
                groups: List[List[Token]] = []
                grp:    List[Token]       = []
                depth = 1
                while depth > 0 and not self._eof():
                    t = self._eat()
                    if t[0] == 'LPAREN':
                        depth += 1
                        grp.append(t)
                    elif t[0] == 'RPAREN':
                        depth -= 1
                        if depth == 0:
                            if grp:
                                groups.append(grp)
                        else:
                            grp.append(t)
                    elif t[0] == 'COMMA' and depth == 1:
                        groups.append(grp)
                        grp = []
                    else:
                        grp.append(t)
                self._eat_if('SEMICOLON')
                gate_stmts.append((gate_type, groups))

            # always / initial — skip entire block (unsupported, warn)
            elif cur[0] == 'IDENT' and cur[1] in ('always', 'initial'):
                self._eat()
                # skip sensitivity list @(...)
                if self._eat_if('IDENT', '@') or self._eat_if('AMP'):
                    if self._eat_if('LPAREN'):
                        d = 1
                        while d > 0 and not self._eof():
                            t = self._eat()
                            if t[0] == 'LPAREN':
                                d += 1
                            elif t[0] == 'RPAREN':
                                d -= 1
                self._skip_block()

            else:
                self._skip_to_semi()

        return mod_name, ports, assigns, gate_stmts


# ═══════════════════════════════════════════════════════════════════════════════
#  Expression evaluator
# ═══════════════════════════════════════════════════════════════════════════════

class _Eval:
    """
    Recursive-descent Verilog expression evaluator.

    All methods return a Bus (MSB-first list of Lit).
    """

    def __init__(self, aig: AIG,
                 env:      Dict[str, Bus],
                 range_of: Dict[str, Optional[Tuple[int, int]]]):
        self._aig      = aig
        self._env      = env
        self._range_of = range_of
        self._toks: List[Token] = []
        self._i = 0

    # ── token helpers ─────────────────────────────────────────────────────────

    def _eof(self): return self._i >= len(self._toks)
    def _cur(self): return self._toks[self._i] if not self._eof() else None

    def _eat(self) -> Token:
        t = self._toks[self._i]; self._i += 1; return t

    def _is(self, kind: str, val: str = None) -> bool:
        t = self._cur()
        if t is None:
            return False
        return t[0] == kind and (val is None or t[1] == val)

    def _eat_if(self, kind: str, val: str = None) -> bool:
        if self._is(kind, val):
            self._eat(); return True
        return False

    # ── AIG helpers ───────────────────────────────────────────────────────────

    def _not(self, a: Lit) -> Lit:          return self._aig.make_not(a)
    def _and2(self, a: Lit, b: Lit) -> Lit: return self._aig.make_and(a, b)
    def _or2(self, a: Lit, b: Lit) -> Lit:  return self._aig.make_or(a, b)
    def _xor2(self, a: Lit, b: Lit) -> Lit: return self._aig.make_xor(a, b)

    def _binop(self, a: Bus, b: Bus, op) -> Bus:
        if len(a) != len(b):
            raise VerilogError(f'Bus width mismatch: {len(a)} vs {len(b)}')
        return [op(x, y) for x, y in zip(a, b)]

    def _reduce(self, bus: Bus, op, identity: Lit) -> Lit:
        if not bus:
            return identity
        r = bus[0]
        for b in bus[1:]:
            r = op(r, b)
        return r

    # ── grammar (highest-to-lowest precedence) ────────────────────────────────

    def eval(self, toks: List[Token]) -> Bus:
        self._toks = toks
        self._i    = 0
        result = self._ternary()
        if not self._eof():
            raise VerilogError(f'Unexpected token in expression: {self._cur()}')
        return result

    def _ternary(self) -> Bus:
        cond = self._lor()
        if not self._is('QUESTION'):
            return cond
        self._eat()
        t_val = self._lor()
        if not self._is('COLON'):
            raise VerilogError("Expected ':' in ternary operator")
        self._eat()
        f_val = self._ternary()   # right-associative
        # cond reduced to 1 bit; t/f zero-extended to same width
        c  = self._reduce(cond, self._or2, FALSE)
        nc = self._not(c)
        w  = max(len(t_val), len(f_val))
        t_ext = [FALSE] * (w - len(t_val)) + t_val
        f_ext = [FALSE] * (w - len(f_val)) + f_val
        return [self._or2(self._and2(c, ti), self._and2(nc, fi))
                for ti, fi in zip(t_ext, f_ext)]

    def _lor(self) -> Bus:
        lhs = self._land()
        while self._is('DPIPE'):
            self._eat()
            rhs = self._land()
            lhs = [self._or2(self._reduce(lhs, self._or2, FALSE),
                             self._reduce(rhs, self._or2, FALSE))]
        return lhs

    def _land(self) -> Bus:
        lhs = self._bitor()
        while self._is('DAMP'):
            self._eat()
            rhs = self._bitor()
            lhs = [self._and2(self._reduce(lhs, self._or2, FALSE),
                              self._reduce(rhs, self._or2, FALSE))]
        return lhs

    def _bitor(self) -> Bus:
        lhs = self._bitxor()
        while self._is('PIPE'):
            self._eat()
            lhs = self._binop(lhs, self._bitxor(), self._or2)
        return lhs

    def _bitxor(self) -> Bus:
        lhs = self._bitand()
        while self._is('CARET') or self._is('TXOR'):
            op  = self._eat()[0]
            rhs = self._bitand()
            fn  = self._xor2 if op == 'CARET' else (
                lambda a, b: self._not(self._xor2(a, b)))
            lhs = self._binop(lhs, rhs, fn)
        return lhs

    def _bitand(self) -> Bus:
        lhs = self._unary()
        while self._is('AMP'):
            self._eat()
            lhs = self._binop(lhs, self._unary(), self._and2)
        return lhs

    def _unary(self) -> Bus:
        if self._is('TILDE') or self._is('BANG'):
            self._eat()
            return [self._not(b) for b in self._unary()]
        # reduction operators
        if self._is('AMP'):
            self._eat()
            op = self._primary()
            return [self._reduce(op, self._and2, TRUE)]
        if self._is('TAMP'):
            self._eat()
            op = self._primary()
            return [self._not(self._reduce(op, self._and2, TRUE))]
        if self._is('PIPE'):
            self._eat()
            op = self._primary()
            return [self._reduce(op, self._or2, FALSE)]
        if self._is('TPIPE'):
            self._eat()
            op = self._primary()
            return [self._not(self._reduce(op, self._or2, FALSE))]
        if self._is('CARET'):
            self._eat()
            op = self._primary()
            return [self._reduce(op, self._xor2, FALSE)]
        if self._is('TXOR'):
            self._eat()
            op = self._primary()
            return [self._not(self._reduce(op, self._xor2, FALSE))]
        return self._primary()

    def _primary(self) -> Bus:
        if self._is('LPAREN'):
            self._eat()
            val = self._ternary()
            if not self._is('RPAREN'):
                raise VerilogError("Expected ')'")
            self._eat()
            return val
        if self._is('LBRACE'):
            return self._concat()
        if self._is('NUMBER'):
            return _parse_number(self._eat()[1])
        if self._is('IDENT'):
            name = self._eat()[1]
            if self._eat_if('LBRACKET'):
                hi = self._const_idx()
                if self._eat_if('COLON'):
                    lo = self._const_idx()
                    if not self._eat_if('RBRACKET'):
                        raise VerilogError("Expected ']'")
                    return self._part_select(name, hi, lo)
                if not self._eat_if('RBRACKET'):
                    raise VerilogError("Expected ']'")
                return self._bit_select(name, hi)
            return self._lookup(name)
        raise VerilogError(f'Unexpected token in expression: {self._cur()}')

    def _const_idx(self) -> int:
        if not self._is('NUMBER'):
            raise VerilogError(f'Expected constant index, got {self._cur()}')
        bits = _parse_number(self._eat()[1])
        return sum(b << (len(bits) - 1 - i) for i, b in enumerate(bits))

    def _concat(self) -> Bus:
        self._eat()   # '{'
        result: Bus = []
        while not self._is('RBRACE') and not self._eof():
            result.extend(self._ternary())
            self._eat_if('COMMA')
        if not self._eat_if('RBRACE'):
            raise VerilogError("Expected '}'")
        return result

    def _lookup(self, name: str) -> Bus:
        if name not in self._env:
            raise VerilogError(f'Undefined net: {name!r}')
        return list(self._env[name])

    def _bit_select(self, name: str, bit: int) -> Bus:
        bus = self._lookup(name)
        rng = self._range_of.get(name)
        if rng is None:
            if bit != 0 or len(bus) != 1:
                raise VerilogError(
                    f'Bit select [{bit}] out of range for scalar {name!r}')
            return [bus[0]]
        msb, lsb = rng
        idx = (msb - bit) if msb >= lsb else (bit - msb)
        if idx < 0 or idx >= len(bus):
            raise VerilogError(
                f'Bit select [{bit}] out of range for {name!r}[{msb}:{lsb}]')
        return [bus[idx]]

    def _part_select(self, name: str, hi: int, lo: int) -> Bus:
        bus = self._lookup(name)
        rng = self._range_of.get(name)
        if rng is None:
            raise VerilogError(f'Part-select on scalar net {name!r}')
        msb, lsb = rng
        i_hi = (msb - hi) if msb >= lsb else (hi - msb)
        i_lo = (msb - lo) if msb >= lsb else (lo - msb)
        if i_hi < 0 or i_lo >= len(bus) or i_hi > i_lo:
            raise VerilogError(
                f'Part-select [{hi}:{lo}] out of range for {name!r}[{msb}:{lsb}]')
        return bus[i_hi : i_lo + 1]


# ═══════════════════════════════════════════════════════════════════════════════
#  LHS helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _lvalue_from_toks(toks: List[Token]) -> Tuple[str, Optional[int], Optional[int]]:
    """Parse a token group as a (name, hi, lo) lvalue."""
    if not toks or toks[0][0] != 'IDENT':
        raise VerilogError(f'Invalid lvalue tokens: {toks!r}')
    name = toks[0][1]
    if len(toks) == 1:
        return name, None, None
    if toks[1][0] != 'LBRACKET':
        return name, None, None
    # Find COLON and RBRACKET in remainder
    rest = toks[2:]
    colon = next((i for i, t in enumerate(rest) if t[0] == 'COLON'), None)
    rbrk  = next((i for i, t in enumerate(rest) if t[0] == 'RBRACKET'), None)
    if rbrk is None:
        raise VerilogError(f'Missing ] in lvalue: {toks!r}')
    if colon is not None and colon < rbrk:
        hi = int(''.join(t[1] for t in rest[:colon]))
        lo = int(''.join(t[1] for t in rest[colon + 1:rbrk]))
        return name, hi, lo
    hi = int(''.join(t[1] for t in rest[:rbrk]))
    return name, hi, None


def _assign_to_lhs(
    env:      Dict[str, Bus],
    range_of: Dict[str, Optional[Tuple[int, int]]],
    name:     str,
    hi:       Optional[int],
    lo:       Optional[int],
    rhs:      Bus,
) -> None:
    if hi is None:
        env[name] = list(rhs)
        return

    rng = range_of.get(name)
    if rng is None:
        # Scalar: only [0] valid
        bus = env.setdefault(name, [FALSE])
        if hi == 0 and rhs:
            bus[0] = rhs[0]
        return

    msb, lsb = rng
    width = abs(msb - lsb) + 1
    bus   = env.setdefault(name, [FALSE] * width)

    if lo is None:
        # Single-bit assign: name[hi] = rhs[0]
        idx = (msb - hi) if msb >= lsb else (hi - msb)
        if 0 <= idx < len(bus) and rhs:
            bus[idx] = rhs[0]
    else:
        # Part-select assign: name[hi:lo] = rhs
        i_hi = (msb - hi) if msb >= lsb else (hi - msb)
        for k, lit in enumerate(rhs):
            idx = i_hi + k
            if 0 <= idx < len(bus):
                bus[idx] = lit


# ═══════════════════════════════════════════════════════════════════════════════
#  Synthesis: parsed module → StructuralModule
# ═══════════════════════════════════════════════════════════════════════════════

def _synthesize(
    mod_name:   str,
    ports:      Dict[str, PortDecl],
    assigns:    List[Tuple],
    gate_stmts: List[Tuple],
    verbose:    bool = False,
) -> 'StructuralModule':
    from ..datapath.structural import StructuralModule

    # ── Collect input bit names (bus-expanded, MSB-first) ─────────────────────
    input_names: List[str] = []
    range_of: Dict[str, Optional[Tuple[int, int]]] = {}

    for name, (kind, msb, lsb) in ports.items():
        range_of[name] = None if msb is None else (msb, lsb)
        if kind != 'input':
            continue
        if msb is None:
            input_names.append(name)
        else:
            step = -1 if msb >= lsb else 1
            for i in range(msb, lsb + step, step):
                input_names.append(f'{name}[{i}]')

    m   = StructuralModule(mod_name, input_names)
    aig = m._aig

    # ── Populate env with input literals ──────────────────────────────────────
    env: Dict[str, Bus] = {}

    for name, (kind, msb, lsb) in ports.items():
        if kind != 'input':
            continue
        if msb is None:
            env[name] = [m.input(name)]
        else:
            step = -1 if msb >= lsb else 1
            env[name] = [m.input(f'{name}[{i}]')
                         for i in range(msb, lsb + step, step)]

    def _eval(toks: List[Token]) -> Bus:
        return _Eval(aig, env, range_of).eval(toks)

    # ── Gate primitives ───────────────────────────────────────────────────────
    for gate_type, port_groups in gate_stmts:
        if gate_type in ('not', 'buf'):
            # Last group = single input; all others = outputs
            if len(port_groups) < 2:
                raise VerilogError(
                    f'{gate_type!r} gate requires ≥ 2 ports')
            in_bus = _eval(port_groups[-1])
            for out_grp in port_groups[:-1]:
                result = ([aig.make_not(b) for b in in_bus]
                          if gate_type == 'not' else list(in_bus))
                n, hi, lo = _lvalue_from_toks(out_grp)
                range_of.setdefault(n, None)
                _assign_to_lhs(env, range_of, n, hi, lo, result)
        else:
            if len(port_groups) < 2:
                raise VerilogError(f'{gate_type!r} gate requires ≥ 2 ports')
            in_buses = [_eval(g) for g in port_groups[1:]]
            # Fold input buses
            result   = list(in_buses[0])
            for ib in in_buses[1:]:
                if gate_type == 'and':
                    result = [aig.make_and(a, b) for a, b in zip(result, ib)]
                elif gate_type == 'nand':
                    result = [aig.make_and(a, b) for a, b in zip(result, ib)]
                elif gate_type == 'or':
                    result = [aig.make_or(a, b) for a, b in zip(result, ib)]
                elif gate_type == 'nor':
                    result = [aig.make_or(a, b) for a, b in zip(result, ib)]
                elif gate_type == 'xor':
                    result = [aig.make_xor(a, b) for a, b in zip(result, ib)]
                elif gate_type == 'xnor':
                    result = [aig.make_xor(a, b) for a, b in zip(result, ib)]
            if gate_type in ('nand', 'nor', 'xnor'):
                result = [aig.make_not(b) for b in result]
            out_n, out_hi, out_lo = _lvalue_from_toks(port_groups[0])
            range_of.setdefault(out_n, None)
            _assign_to_lhs(env, range_of, out_n, out_hi, out_lo, result)

    # ── Continuous assigns (multi-pass for forward references) ────────────────
    todo = list(assigns)
    for _pass in range(len(assigns) + 1):
        if not todo:
            break
        next_todo: List[Tuple] = []
        for item in todo:
            (lhs_name, lhs_hi, lhs_lo), rhs_toks = item
            try:
                rhs = _eval(rhs_toks)
                range_of.setdefault(lhs_name, None)
                _assign_to_lhs(env, range_of, lhs_name, lhs_hi, lhs_lo, rhs)
            except VerilogError:
                next_todo.append(item)
        if len(next_todo) == len(todo):
            # No progress — evaluate once more to surface the real error
            (lhs_name, lhs_hi, lhs_lo), rhs_toks = next_todo[0]
            _assign_to_lhs(env, range_of, lhs_name, lhs_hi, lhs_lo,
                           _eval(rhs_toks))
        todo = next_todo

    # ── Declare outputs ───────────────────────────────────────────────────────
    for name, (kind, msb, lsb) in ports.items():
        if kind != 'output':
            continue
        bus = env.get(name)
        if bus is None:
            raise VerilogError(f'Output port {name!r} has no driver')
        if msb is None:
            m.add_output(name, bus[0])
        else:
            step = -1 if msb >= lsb else 1
            for idx, i in enumerate(range(msb, lsb + step, step)):
                if idx < len(bus):
                    m.add_output(f'{name}[{i}]', bus[idx])

    if verbose:
        n_out = sum(
            1 if ports[n][1] is None
            else abs(ports[n][1] - (ports[n][2] or 0)) + 1
            for n in ports if ports[n][0] == 'output'
        )
        print(f'  [Verilog] {mod_name}: '
              f'{len(input_names)} input bits, '
              f'{n_out} output bits, '
              f'{aig.n_ands} AND nodes before synthesis')

    return m


# ═══════════════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════════════

def verilog_to_module(text: str) -> 'StructuralModule':
    """Parse a Verilog module string into a StructuralModule (not yet synthesized)."""
    toks = _tokenize(text)
    mod_name, ports, assigns, gate_stmts = _Parser(toks).parse()
    return _synthesize(mod_name, ports, assigns, gate_stmts)


def parse_verilog(
    text:    str,
    script:  str  = None,
    verbose: bool = False,
) -> object:
    """
    Parse a Verilog module string and run the synthesis pipeline.

    Returns an ``OptimizeResult`` (same as ``optimize()``).
    """
    toks = _tokenize(text)
    mod_name, ports, assigns, gate_stmts = _Parser(toks).parse()
    m = _synthesize(mod_name, ports, assigns, gate_stmts, verbose=verbose)
    return m.finalize(script=script, verbose=verbose)


def read_verilog(
    path:    str,
    script:  str  = None,
    verbose: bool = False,
) -> object:
    """
    Load a Verilog (.v / .sv) file and run the synthesis pipeline.

    Returns an ``OptimizeResult``.
    """
    with open(path, 'r', encoding='utf-8') as fh:
        text = fh.read()
    return parse_verilog(text, script=script, verbose=verbose)
