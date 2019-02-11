#!/usr/bin/env python
"""
typed_arith_parse.py: Parse shell-like and C-like arithmetic.
"""
from __future__ import print_function

import sys

from asdl import tdop
from asdl.tdop import CompositeNode

from _devbuild.gen import typed_arith_asdl
from _devbuild.gen.typed_arith_asdl import arith_expr_t
from _devbuild.gen.typed_arith_asdl import arith_expr__ArithVar
from _devbuild.gen.typed_arith_asdl import arith_expr__Const
from asdl.tdop import Parser
from typing import Union
from _devbuild.gen.typed_arith_asdl import arith_expr__ArithBinary
from _devbuild.gen.typed_arith_asdl import arith_expr__Index
from _devbuild.gen.typed_arith_asdl import arith_expr__FuncCall
from asdl.tdop import ParserSpec

arith_expr = typed_arith_asdl.arith_expr
Token = tdop.Token


#
# Null Denotation -- token that takes nothing on the left
#

def NullConstant(p,  # type: Parser
                 token,  # type: Token
                 bp,  # type: int
                 ):
  # type: (...) -> arith_expr_t
  if token.type == 'number':
    return arith_expr.Const(token.val)
  # We have to wrap a string in some kind of variant.
  if token.type == 'name':
    return arith_expr.ArithVar(token.val)

  raise AssertionError(token.type)


def NullParen(p,  # type: Parser
              token,  # type: Token
              bp,  # type: int
              ):
  # type: (...) -> arith_expr_t
  """ Arithmetic grouping """
  r = p.ParseUntil(bp)
  p.Eat(')')
  return r


def NullPrefixOp(p, token, bp):
  # type: (Parser, Token, int) -> arith_expr_t
  """Prefix operator.

  Low precedence:  return, raise, etc.
    return x+y is return (x+y), not (return x) + y

  High precedence: logical negation, bitwise complement, etc.
    !x && y is (!x) && y, not !(x && y)
  """
  r = p.ParseUntil(bp)
  return arith_expr.ArithUnary(token.val, r)


def NullIncDec(p, token, bp):
  # type: (Parser, Token, int) -> arith_expr_t
  """ ++x or ++x[1] """
  right = p.ParseUntil(bp)
  if not isinstance(right, (arith_expr.ArithVar, arith_expr.Index)):
    raise tdop.ParseError("Can't assign to %r" % right)
  return arith_expr.ArithUnary(token.val, right)


#
# Left Denotation -- token that takes an expression on the left
#

def LeftIncDec(p,  # type: Parser
               token,  # type: Token
               left,  # type: arith_expr_t
               rbp,  # type: int
               ):
  # type: (...) -> arith_expr_t
  """ For i++ and i--
  """
  if not isinstance(left, (arith_expr.ArithVar, arith_expr.Index)):
    raise tdop.ParseError("Can't assign to %r" % left)
  token.type = 'post' + token.type
  return arith_expr.ArithUnary(token.val, left)


def LeftIndex(p, token, left, unused_bp):
  # type: (Parser, Token, arith_expr_t, int) -> arith_expr_t
  """ index f[x+1] """
  # f[x] or f[x][y]
  if not isinstance(left, arith_expr.ArithVar):
    raise tdop.ParseError("%s can't be indexed" % left)
  index = p.ParseUntil(0)
  if p.AtToken(':'):
    p.Next()
    end = p.ParseUntil(0)  # type: Union[arith_expr_t, None]
  else:
    end = None

  p.Eat(']')

  # TODO: If you see ], then
  # 1:4
  # 1:4:2
  # Both end and step are optional

  if end:
    return arith_expr.Slice(left, index, end, None)
  else:
    return arith_expr.Index(left, index)


def LeftTernary(p,  # type: Parser
                token,  # type: Token
                left,  # type: Union[arith_expr__ArithBinary, arith_expr__Const]
                bp,  # type: int
                ):
  # type: (...) -> arith_expr_t
  """ e.g. a > 1 ? x : y """
  true_expr = p.ParseUntil(bp)
  p.Eat(':')
  false_expr = p.ParseUntil(bp)
  return arith_expr.Ternary(left, true_expr, false_expr)


def LeftBinaryOp(p,  # type: Parser
                 token,  # type: Token
                 left,  # type: Union[arith_expr__ArithBinary, arith_expr__Const, arith_expr__FuncCall]
                 rbp,  # type: int
                 ):
  # type: (...) -> arith_expr__ArithBinary
  """ Normal binary operator like 1+2 or 2*3, etc. """
  return arith_expr.ArithBinary(token.val, left, p.ParseUntil(rbp))


def LeftAssign(p,  # type: Parser
               token,  # type: Token
               left,  # type: arith_expr__ArithVar
               rbp,  # type: int
               ):
  # type: (...) -> arith_expr__ArithBinary
  """ Normal binary operator like 1+2 or 2*3, etc. """
  # x += 1, or a[i] += 1
  if not isinstance(left, (arith_expr.ArithVar, arith_expr.Index)):
    raise tdop.ParseError("Can't assign to %r" % left)
  return arith_expr.ArithBinary(token.val, left, p.ParseUntil(rbp))


# For overloading of , inside function calls
COMMA_PREC = 1

def LeftFuncCall(p, token, left, unused_bp):
  # type: (Parser, Token, arith_expr_t, int) -> arith_expr__FuncCall
  """ Function call f(a, b). """
  args = []
  # f(x) or f[i](x)
  if not isinstance(left, arith_expr.ArithVar):
    raise tdop.ParseError("%s can't be called" % left)
  func_name = left.name  # get a string

  while not p.AtToken(')'):
    # We don't want to grab the comma, e.g. it is NOT a sequence operator.  So
    # set the precedence to 5.
    args.append(p.ParseUntil(COMMA_PREC))
    if p.AtToken(','):
      p.Next()
  p.Eat(")")
  return arith_expr.FuncCall(func_name, args)


def MakeShellParserSpec():
  # type: () -> ParserSpec
  """
  Create a parser.

  Compare the code below with this table of C operator precedence:
  http://en.cppreference.com/w/c/language/operator_precedence
  """
  spec = tdop.ParserSpec()

  spec.Left(31, LeftIncDec, ['++', '--'])
  spec.Left(31, LeftFuncCall, ['('])
  spec.Left(31, LeftIndex, ['['])

  # 29 -- binds to everything except function call, indexing, postfix ops
  spec.Null(29, NullIncDec, ['++', '--'])
  spec.Null(29, NullPrefixOp, ['+', '!', '~', '-'])

  # Right associative: 2 ** 3 ** 2 == 2 ** (3 ** 2)
  spec.LeftRightAssoc(27, LeftBinaryOp, ['**'])
  spec.Left(25, LeftBinaryOp, ['*', '/', '%'])

  spec.Left(23, LeftBinaryOp, ['+', '-'])
  spec.Left(21, LeftBinaryOp, ['<<', '>>'])
  spec.Left(19, LeftBinaryOp, ['<', '>', '<=', '>='])
  spec.Left(17, LeftBinaryOp, ['!=', '=='])

  spec.Left(15, LeftBinaryOp, ['&'])
  spec.Left(13, LeftBinaryOp, ['^'])
  spec.Left(11, LeftBinaryOp, ['|'])
  spec.Left(9, LeftBinaryOp, ['&&'])
  spec.Left(7, LeftBinaryOp, ['||'])

  spec.LeftRightAssoc(5, LeftTernary, ['?'])

  # Right associative: a = b = 2 is a = (b = 2)
  spec.LeftRightAssoc(3, LeftAssign, [
      '=',
      '+=', '-=', '*=', '/=', '%=',
      '<<=', '>>=', '&=', '^=', '|='])

  spec.Left(COMMA_PREC, LeftBinaryOp, [','])

  # 0 precedence -- doesn't bind until )
  spec.Null(0, NullParen, ['('])  # for grouping

  # -1 precedence -- never used
  spec.Null(-1, NullConstant, ['name', 'number'])
  spec.Null(-1, tdop.NullError, [')', ']', ':', 'eof'])

  return spec


def MakeParser(s):
  # type: (str) -> Parser
  """Used by tests."""
  spec = MakeShellParserSpec()
  lexer = tdop.Tokenize(s)
  p = tdop.Parser(spec, lexer)
  return p


def ParseShell(s, expected=None):
  """Used by tests."""
  p = MakeParser(s)
  tree = p.Parse()

  sexpr = repr(tree)
  if expected is not None:
    assert sexpr == expected, '%r != %r' % (sexpr, expected)

  #print('%-40s %s' % (s, sexpr))
  return tree


def main(argv):
  try:
    s = argv[1]
  except IndexError:
    print('Usage: ./arith_parse.py EXPRESSION')
  else:
    try:
      tree = ParseShell(s)
    except tdop.ParseError as e:
      print('Error parsing %r: %s' % (s, e), file=sys.stderr)
    print(tree)


if __name__ == '__main__':
  main(sys.argv)
