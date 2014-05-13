import enum
import jqsh.filter
import string
import unicodedata

TokenType = enum.Enum('TokenType', [
    'close_array',
    'close_paren',
    'comment',
    'illegal',
    'open_array',
    'open_paren',
    'trailing_whitespace'
], module=__name__)

class Token:
    def __eq__(self, other):
        return self.type is other.type and self.text == other.text
    
    def __init__(self, token_type, token_string=None, text=None):
        self.type = token_type
        self.string = token_string # ''.join(token.string for token in tokenize(jqsh_string)) == jqsh_string
        self.text = text # metadata like the name of a name token or the digits of a number literal. None for simple tokens
    
    def __repr__(self):
        return 'jqsh.parser.Token(' + repr(self.type) + ('' if self.string is None else ', token_string=' + repr(self.string)) + ('' if self.text is None else ', text=' + repr(self.text)) + ')'
    
    def __str__(self):
        if self.string is None:
            return "'" + repr(self) + "'"
        else:
            return self.string

matching_parens = { # a dictionary that maps opening parenthesis-like tokens (parens) to the associated closing parens
    TokenType.open_array: TokenType.close_array,
    TokenType.open_paren: TokenType.close_paren
}

paren_filters = {
    TokenType.open_array: jqsh.filter.Array,
    TokenType.open_paren: jqsh.filter.Parens
}

symbols = {
    '(': TokenType.open_paren,
    ')': TokenType.close_paren,
    '[': TokenType.open_array,
    ']': TokenType.close_array
}

def parse(tokens):
    if isinstance(tokens, str):
        tokens = list(tokenize(tokens))
    tokens = [token for token in tokens if isinstance(token, jqsh.filter.Filter) or token.type is not TokenType.comment]
    if not len(tokens):
        return jqsh.filter.Filter() # token list is empty, return an empty filter
    for token in tokens:
        if token.type is TokenType.illegal:
            raise SyntaxError('illegal character: ' + repr(token.text[0]) + ' (U+' + format(ord(token.text[0]), 'x').upper() + ' ' + unicodedata.name(token.text[0], 'unknown character') + ')')
    if isinstance(tokens[-1], Token) and tokens[-1].type is TokenType.trailing_whitespace:
        if len(tokens) == 1:
            return jqsh.filter.Filter() # token list consists entirely of whitespace, return an empty filter
        else:
            tokens[-2].string += tokens[-1].string # merge the trailing whitespace into the second-to-last token
            tokens.pop() # remove the trailing_whitespace token
    paren_balance = 0
    paren_start = None
    for i, token in reversed(list(enumerate(tokens))): # iterating over the token list in reverse because we modify it in the process
        if not isinstance(token, Token):
            continue
        elif token.type in matching_parens.values():
            if paren_balance == 0:
                paren_start = i
            paren_balance += 1
        elif token.type in matching_parens.keys():
            paren_balance -= 1
            if paren_balance < 0:
                raise SyntaxError('too many opening parens of type ' + repr(token.type))
            elif paren_balance == 0:
                if matching_parens[token.type] is tokens[paren_start].type:
                    tokens[i:paren_start + 1] = [paren_filters[token.type](attribute=parse(tokens[i + 1:paren_start]))] # parse the inside of the parens
                else:
                    raise SyntaxError('opening paren of type ' + repr(token.type) + ' does not match closing paren of type ' + repr(tokens[paren_start].type))
                paren_start = None
    if paren_balance != 0:
        raise SyntaxError('mismatched parens')
    if len(tokens) == 1 and isinstance(tokens[0], jqsh.filter.Filter):
        return tokens[0] # finished parsing
    else:
        raise SyntaxError('could not parse token list')

def tokenize(jqsh_string):
    rest_string = jqsh_string
    if not isinstance(rest_string, str):
        rest_string = rest_string.decode('utf-8')
    whitespace_prefix = ''
    if rest_string.startswith('\ufeff'):
        whitespace_prefix += rest_string[0]
        rest_string = rest_string[1:]
    while len(rest_string):
        if rest_string[0] in string.whitespace:
            whitespace_prefix += rest_string[0]
            rest_string = rest_string[1:]
        elif rest_string[0] == '#':
            rest_string = rest_string[1:]
            comment = ''
            while len(rest_string):
                if rest_string[0] == '\n':
                    break
                comment += rest_string[0]
                rest_string = rest_string[1:]
            yield Token(TokenType.comment, token_string=whitespace_prefix + '#' + comment, text=comment)
            whitespace_prefix = ''
        elif any(rest_string.startswith(symbol) for symbol in symbols):
            for symbol, token_type in sorted(symbols.items(), key=lambda pair: -len(pair[0])): # look at longer symbols first, so that a += is not mistakenly tokenized as a +
                if rest_string.startswith(symbol):
                    yield Token(token_type, token_string=whitespace_prefix + rest_string[:len(symbol)])
                    whitespace_prefix = ''
                    rest_string = rest_string[len(symbol):]
                    break
        else:
            yield Token(TokenType.illegal, token_string=whitespace_prefix + rest_string, text=rest_string)
            whitespace_prefix = ''
            rest_string = ''
    if len(whitespace_prefix):
        yield Token(TokenType.trailing_whitespace, token_string=whitespace_prefix)