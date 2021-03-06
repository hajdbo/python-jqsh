import sys

import contextlib
import copy
import decimal
import itertools
import jqsh.channel
import jqsh.functions
import jqsh.values
import more_itertools
import subprocess
import threading
import traceback

class NotAllowed(Exception):
    pass

class FilterThread(threading.Thread):
    def __init__(self, the_filter, input_channel=None):
        super().__init__(name='jqsh FilterThread')
        self.filter = the_filter
        self.input_channel = jqsh.channel.Channel(terminated=True) if input_channel is None else input_channel
        self.output_channel = jqsh.channel.Channel()
    
    def run(self):
        self.filter.run_raw(self.input_channel, self.output_channel)

class Filter:
    """Filters are the basic building block of the jqsh language. This base class implements the empty filter."""
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '()'
    
    def __str__(self):
        """The filter's representation in jqsh."""
        return ''
    
    def assign(self, value_channel, input_channel, output_channel):
        raise NotImplementedError('cannot assign to this filter')
    
    def run(self, input_channel):
        """This is called from run_raw, and should be overridden by subclasses.
        
        Yielded values are pushed onto the output channel, and it is terminated on return. Exceptions are handled by run_raw.
        """
        return
        yield # the empty generator #FROM http://stackoverflow.com/a/13243870/667338
    
    def run_raw(self, input_channel, output_channel):
        """This is called from the filter thread, and may be overridden by subclasses instead of run."""
        def run_thread(bridge):
            try:
                for value in self.run(input_channel=bridge):
                    output_channel.push(value)
                    if isinstance(value, jqsh.values.JQSHException):
                        break
            except Exception as e:
                output_channel.throw(jqsh.values.JQSHException('internal', python_exception=e, exc_info=sys.exc_info(), traceback_string=traceback.format_exc()))
        
        bridge_channel = jqsh.channel.Channel()
        helper_thread = threading.Thread(target=run_thread, kwargs={'bridge': bridge_channel})
        handle_namespaces = threading.Thread(target=input_channel.push_namespaces, args=(bridge_channel, output_channel))
        helper_thread.start()
        handle_namespaces.start()
        for value in input_channel:
            bridge_channel.push(value)
            if isinstance(value, jqsh.values.JQSHException):
                output_channel.throw(value)
                break
        bridge_channel.terminate()
        helper_thread.join()
        handle_namespaces.join()
        output_channel.terminate()
    
    def sensible_string(self, input_channel=None):
        ret = next(self.start(input_channel))
        if isinstance(ret, jqsh.values.String):
            return ret.value
        else:
            raise TypeError('got a ' + ret.__class__.__name__ + ', expected a string')
    
    def start(self, input_channel=None):
        filter_thread = FilterThread(self, input_channel=input_channel)
        filter_thread.start()
        return filter_thread.output_channel

class Parens(Filter):
    def __init__(self, attribute=Filter()):
        self.attribute = attribute
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + ('' if self.attribute.__class__ == Filter else repr(self.attribute)) + ')'
    
    def __str__(self):
        return '(' + str(self.attribute) + ')'
    
    def run(self, input_channel):
        yield from self.attribute.start(input_channel)

class Array(Parens):
    def __str__(self):
        return '[' + str(self.attribute) + ']'
    
    def run(self, input_channel):
        yield jqsh.values.Array(self.attribute.start(input_channel))

class Object(Parens):
    def __str__(self):
        return '{' + str(self.attribute) + '}'
    
    def run(self, input_channel):
        #TODO handle shorthand keys and sensible strings
        obj = jqsh.values.Object(terminated=False)
        for value in self.attribute.start(input_channel):
            try:
                obj.push(value)
            except TypeError:
                yield jqsh.values.JQSHException('type')
            except ValueError:
                yield jqsh.values.JQSHException('length')
        obj.terminate()
        yield obj

class Conditional(Filter):
    def __init__(self, attributes):
        self.attributes = list(attributes)
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + repr(self.attributes) + ')'
    
    def __str__(self):
        return ' '.join(attribute_name + ' ' + str(attribute_value) for attribute_name, attribute_value in self.attributes) + ' end'
    
    def run(self, input_channel):
        for attribute_name, attribute_value in self.attributes:
            if attribute_name in ('if', 'elif', 'elseIf'):
                input_channel, conditional_input = input_channel / 2
                try:
                    next_value = next(attribute_value.start(conditional_input))
                    if isinstance(next_value, jqsh.values.JQSHException):
                        yield next_value
                        return
                    conditional = bool(next_value)
                except StopIteration:
                    yield jqsh.values.JQSHException('empty')
                    return
            elif attribute_name == 'then':
                if not conditional:
                    continue
                yield from attribute_value.start(input_channel)
            elif attribute_name == 'else':
                if conditional:
                    continue
                yield from attribute_value.start(input_channel)
            else:
                raise NotImplementedError('unknown clause in if filter')

class Try(Conditional):
    def run(self, input_channel):
        exception_handlers = {}
        default_handler = None
        else_handler = None
        exception_names = []
        for attribute_name, attribute_value in self.attributes:
            if attribute_name == 'try':
                try_block = attribute_value
            elif attribute_name == 'catch':
                input_channel, exception_name_input = input_channel / 2
                try:
                    exception_names.append(attribute_value.sensible_string(exception_name_input))
                except (StopIteration, TypeError):
                    yield jqsh.values.JQSHException('sensibleString')
                    return
            elif attribute_name == 'then':
                for exception_name in exception_names:
                    exception_handlers[exception_name] = attribute_value
            elif attribute_name == 'except':
                default_handler = attribute_value
            elif attribute_name == 'else':
                else_handler = attribute_value
        try_input, except_input = input_channel / 2
        try_output, ret = try_block.start(try_input) / 2
        for value in try_output:
            if isinstance(value, jqsh.values.JQSHException):
                if value.name in exception_handlers:
                    yield from exception_handlers[value.name].start(except_input) #TODO modify context to allow re-raise
                    return
                elif default_handler is not None:
                    yield from default_handler.start(except_input) #TODO modify context to allow re-raise
                    return
                else:
                    yield value
                    return
        if else_handler is None:
            yield from ret
        else:
            yield from else_handler.start(except_input)

class Name(Filter):
    def __init__(self, name):
        self.name = name
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + repr(self.name) + ')'
    
    def __str__(self):
        return self.name
    
    def assign(self, value_channel, input_channel, output_channel):
        handle_globals = threading.Thread(target=input_channel.push_attribute, args=('global_namespace', output_channel))
        handle_format_strings = threading.Thread(target=input_channel.push_attribute, args=('format_strings', output_channel))
        handle_context = threading.Thread(target=input_channel.push_attribute, args=('context', output_channel))
        handle_values = threading.Thread(target=output_channel.pull, args=(input_channel,))
        handle_globals.start()
        handle_format_strings.start()
        handle_context.start()
        handle_values.start()
        input_locals = copy.copy(input_channel.local_namespace)
        var = list(value_channel)
        for value in var:
            if isinstance(value, jqsh.values.JQSHException):
                output_channel.throw(value)
                break
        else:
            input_locals[self.name] = var
        output_channel.local_namespace = input_locals
        output_channel.terminate()
        handle_globals.join()
        handle_format_strings.join()
        handle_context.join()
        handle_values.join()
    
    def run_raw(self, input_channel, output_channel):
        if self.name in input_channel.local_namespace:
            handle_namespaces = threading.Thread(target=input_channel.push_namespaces, args=(output_channel,))
            handle_namespaces.start()
            for value in input_channel.local_namespace[self.name]:
                output_channel.push(value)
            output_channel.terminate()
            handle_namespaces.join()
        else:
            try:
                builtin = input_channel.context.get_builtin(self.name)
            except KeyError:
                output_channel.throw(jqsh.values.JQSHException('numArgs', function_name=self.name, expected=set(jqsh.functions.builtin_functions[self.name]), received=0) if self.name in jqsh.functions.builtin_functions else jqsh.values.JQSHException('name', missing_name=self.name)) #TODO fix for context-based builtins
            else:
                builtin(input_channel=input_channel, output_channel=output_channel)
    
    def sensible_string(self, input_channel=None):
        return self.name

class NumberLiteral(Filter):
    def __init__(self, number):
        self.number_string = str(number)
        self.number = jqsh.values.Number(number)
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + repr(self.number_string) + ')'
    
    def __str__(self):
        return self.number_string
    
    def run(self, input_channel):
        yield jqsh.values.Number(self.number)

class StringLiteral(Filter):
    def __init__(self, text):
        self.text = text
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + repr(self.text) + ')'
    
    def __str__(self):
        return '"' + ''.join(self.escape(c) for c in self.text) + '"'
    
    @staticmethod
    def escape(character):
        if character == '\b':
            return '\\b'
        elif character == '\t':
            return '\\t'
        elif character == '\n':
            return '\\n'
        elif character == '\f':
            return '\\f'
        elif character == '\r':
            return '\\r'
        elif character == '"':
            return '\\"'
        elif character == '\\':
            return '\\\\'
        elif ord(character) > 0x10000:
            codepoint = ord(character) - 0x10000
            return '\\u{:04x}\\u{:04x}'.format(0xd800 + (codepoint >> 0xa), 0xdc00 + (codepoint & 0x3ff))
        elif ord(character) < 0x20 or ord(character) >= 0x7f:
            return '\\u{:04x}'.format(ord(character))
        else:
            return character
    
    @staticmethod
    def representation(the_string):
        return '"' + ''.join(StringLiteral.escape(character) for character in str(the_string)) + '"'
    
    def run(self, input_channel):
        yield jqsh.values.String(self.text)

class Operator(Filter):
    """Abstract base class for operator filters."""
    
    def __init__(self, *, left=Filter(), right=Filter()):
        self.left_operand = left
        self.right_operand = right
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + ('' if self.left_operand.__class__ == Filter else 'left=' + repr(self.left_operand) + ('' if self.right_operand.__class__ == Filter else ', ')) + ('' if self.right_operand.__class__ == Filter else 'right=' + repr(self.right_operand)) + ')'
    
    def __str__(self):
        return str(self.left_operand) + self.operator_string + str(self.right_operand)
    
    def output_pairs(self, input_channel):
        #TODO don't block until both operands have terminated
        left_input, right_input = input_channel / 2
        left_output = list(self.left_operand.start(left_input))
        right_output = list(self.right_operand.start(right_input))
        if len(left_output) == 0 and len(right_output) == 0:
            return
        elif len(left_output) == 0:
            yield from right_output
        elif len(right_output) == 0:
            yield from left_output
        else:
            for i in range(max(len(left_output), len(right_output))):
                yield left_output[i % len(left_output)], right_output[i % len(right_output)]

class Pipe(Operator): #TODO add correct namespace handling
    operator_string = ' | '
    
    def run(self, input_channel):
        left_output = self.left_operand.start(input_channel)
        yield from self.right_operand.start(left_output)

class Add(Operator):
    operator_string = ' + '
    
    def run(self, input_channel):
        for output in self.output_pairs(input_channel):
            if isinstance(output, tuple):
                left_output, right_output = output
            else:
                yield output
                continue
            if isinstance(left_output, jqsh.values.Number) and isinstance(right_output, jqsh.values.Number):
                yield jqsh.values.Number(left_output + right_output)
            elif isinstance(left_output, jqsh.values.Array) and isinstance(right_output, jqsh.values.Array):
                yield jqsh.values.Array(itertools.chain(left_output, right_output))
            elif isinstance(left_output, jqsh.values.String) and isinstance(right_output, jqsh.values.String):
                yield jqsh.values.String(left_output.value + right_output.value)
            elif all(isinstance(output, jqsh.values.Object) for output in (left_output, right_output)): #TODO fix object handling
                ret = copy.copy(left_output)
                ret.update(right_output)
                yield ret
            else:
                yield jqsh.values.JQSHException('type')

class Apply(Operator):
    operator_string = '.'
    
    def __init__(self, *attributes, left=Filter(), right=Filter()):
        if len(attributes):
            self.attributes = attributes
            self.variadic_form = True
        else:
            self.attributes = [left, right]
            self.variadic_form = False
    
    def __repr__(self):
        if self.variadic_form:
            return 'jqsh.filter.' + self.__class__.__name__ + '(' + ', '.join(repr(attribute) for attribute in self.attributes) + ')'
        else:
            return 'jqsh.filter.' + self.__class__.__name__ + '(' + ('' if self.attributes[0].__class__ == Filter else 'left=' + repr(self.attributes[0]) + ('' if self.attributes[1].__class__ == Filter else ', ')) + ('' if self.attributes[1].__class__ == Filter else 'right=' + repr(self.attributes[1])) + ')'
    
    def __str__(self):
        if self.variadic_form:
            return ' '.join(str(attribute) for attribute in self.attributes)
        else:
            return str(self.attributes[0]) + '.' + str(self.attributes[1])
    
    def run_raw(self, input_channel, output_channel):
        if all(attribute.__class__ == Filter for attribute in self.attributes): # identity function
            output_channel.get_namespaces(input_channel)
            output_channel.pull(input_channel)
        elif len(self.attributes) == 2 and all(attribute.__class__ == NumberLiteral for attribute in self.attributes): # decimal number
            output_channel.push(jqsh.values.Number(str(self.attributes[0]) + '.' + str(self.attributes[1])))
            output_channel.terminate()
            output_channel.get_namespaces(input_channel)
            return
        elif self.attributes[0].__class__ == Filter: # subscripting/lookup on input values
            #TODO support variadic form (recursive run_raw calls)
            input_channel, key_input = input_channel / 2
            try:
                key = next(self.attributes[1].start(key_input))
            except StopIteration:
                output_channel.throw('empty')
                return
            for value in input_channel:
                if isinstance(value, jqsh.values.Object):
                    if key in value:
                        output_channel.push(value[key])
                    else:
                        output_channel.throw('key')
                        return
                elif isinstance(value, jqsh.values.Array):
                    if isinstance(key, jqsh.values.Number):
                        if key % 1 == 0:
                            try:
                                output_channel.push(value[int(key)])
                            except IndexError:
                                output_channel.throw('index')
                                return
                        else:
                            output_channel.throw('integer')
                            return
                    else:
                        output_channel.throw('type')
                        return
                else:
                    output_channel.throw('type')
                    return
            output_channel.terminate()
            output_channel.get_namespaces(input_channel)
            return
        elif self.attributes[0].__class__ == Command: # command with arguments
            try:
                input_channel, string_input, *string_inputs = input_channel / (len(self.attributes) + 1)
                command_name = [self.attributes[0].attribute.sensible_string(string_input)]
                for attribute, string_input in zip(self.attributes[1:], string_inputs):
                    command_name.append(attribute.sensible_string(string_input))
            except (StopIteration, TypeError):
                output_channel.throw('sensibleString')
                return
            for value in Command.run_command(command_name, input_channel):
                output_channel.push(value)
            output_channel.get_namespaces(input_channel)
            output_channel.terminate()
        else: # built-in function with arguments
            input_channel, string_input = input_channel / 2
            try:
                function_name = self.attributes[0].sensible_string(input_channel=string_input)
            except (StopIteration, TypeError):
                output_channel.throw('sensibleString')
                return
            try:
                builtin = input_channel.context.get_builtin(function_name, *self.attributes[1:])
            except KeyError:
                output_channel.throw(jqsh.values.JQSHException('numArgs') if function_name in jqsh.functions.builtin_functions else jqsh.values.JQSHException('name', missing_name=function_name)) #TODO fix for context-based builtins
                return
            else:
                builtin(*self.attributes[1:], input_channel=input_channel, output_channel=output_channel)

class Assign(Operator):
    operator_string = ' = '
    
    def run_raw(self, input_channel, output_channel):
        input_channel, assignment_input = input_channel / 2
        try:
            self.left_operand.assign(self.right_operand.start(input_channel), input_channel=assignment_input, output_channel=output_channel)
        except NotImplementedError:
            output_channel.throw(jqsh.values.JQSHException('assignment', target_filter=self.left_operand))
            return

class Comma(Operator):
    def __str__(self):
        return str(self.left_operand) + ', ' + str(self.right_operand)
    
    def run(self, input_channel):
        left_input, right_input = input_channel / 2
        right_output = self.right_operand.start(right_input)
        yield from self.left_operand.start(left_input)
        yield from right_output

class Multiply(Operator):
    operator_string = ' * '
    
    def run(self, input_channel):
        for output in self.output_pairs(input_channel):
            if isinstance(output, tuple):
                left_output, right_output = output
            else:
                yield output
                continue
            if isinstance(left_output, jqsh.values.Number) and isinstance(right_output, jqsh.values.Number):
                yield jqsh.values.Number(left_output * right_output)
            elif isinstance(left_output, jqsh.values.String) and isinstance(right_output, jqsh.values.Number):
                if right_output % 1 == 0:
                    yield jqsh.values.String(left_output.value * int(right_output))
                else:
                    yield jqsh.values.JQSHException('integer')
            elif isinstance(left_output, jqsh.values.Array) and isinstance(right_output, jqsh.values.Number):
                if right_output % 1 == 0:
                    yield jqsh.values.Array(more_itertools.ncycles(left_output, int(right_output)))
                else:
                    yield jqsh.values.JQSHException('integer')
            elif isinstance(left_output, decimal.Decimal) and isinstance(right_output, jqsh.values.String):
                if left_output % 1 == 0:
                    yield jqsh.values.String(right_output.value * int(left_output))
                else:
                    yield jqsh.values.JQSHException('integer')
            elif isinstance(left_output, decimal.Decimal) and isinstance(right_output, jqsh.values.Array):
                if left_output % 1 == 0:
                    yield jqsh.values.Array(more_itertools.ncycles(right_output, int(left_output)))
                else:
                    yield jqsh.values.JQSHException('integer')
            else:
                yield jqsh.values.JQSHException('type')

class Pair(Operator):
    operator_string = ': '
    
    def run(self, input_channel):
        left_input, right_input = input_channel / 2
        try:
            right_output = next(self.right_operand.start(right_input))
        except StopIteration:
            yield jqsh.values.JQSHException('empty')
            return
        for value in self.left_operand.start(left_input):
            yield jqsh.values.Array((value, right_output))

class Semicolon(Operator):
    operator_string = '; '
    
    def run_raw(self, input_channel, output_channel):
        left_input, right_input = input_channel / 2
        left_output = self.left_operand.start(left_input)
        right_input.get_namespaces(left_output)
        right_output = self.right_operand.start(right_input)
        for value in right_output:
            output_channel.push(value)
        output_channel.terminate()
        output_channel.get_namespaces(right_output)

class UnaryOperator(Filter):
    """Abstract base class for unary-only operator filters."""
    
    def __init__(self, attribute):
        self.attribute = attribute
    
    def __repr__(self):
        return 'jqsh.filter.' + self.__class__.__name__ + '(' + repr(self.attribute) + ')'
    
    def __str__(self):
        return self.operator_string + str(self.attribute)

class Command(UnaryOperator):
    operator_string = '!'
    
    @staticmethod
    def run_command(command_name, input_channel):
        import jqsh.parser
        
        try:
            popen = subprocess.Popen(command_name, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            yield jqsh.values.JQSHException('path')
            return
        except PermissionError:
            yield jqsh.values.JQSHException('permission')
            return
        for value in input_channel:
            popen.stdin.write(str(value).encode('utf-8') + b'\n')
        popen.stdin.write(b'\x04')
        with contextlib.suppress(BrokenPipeError):
            popen.stdin.close()
        try:
            yield from jqsh.parser.parse_json_values(popen.stdout.read().decode('utf-8')) #TODO don't read everything before starting to decode
        except (UnicodeDecodeError, SyntaxError, jqsh.parser.Incomplete):
            yield jqsh.values.JQSHException('commandOutput')
    
    def run(self, input_channel):
        input_channel, attribute_input = input_channel / 2
        try:
            command_name = self.attribute.sensible_string(attribute_input)
        except (StopIteration, TypeError):
            yield jqsh.values.JQSHException('sensibleString')
            return
        yield from self.run_command(command_name, input_channel)

class GlobalVariable(UnaryOperator):
    operator_string = '$'
    
    def assign(self, value_channel, input_channel, output_channel):
        handle_locals = threading.Thread(target=input_channel.push_attribute, args=('local_namespace', output_channel))
        handle_format_strings = threading.Thread(target=input_channel.push_attribute, args=('format_strings', output_channel))
        handle_context = threading.Thread(target=input_channel.push_attribute, args=('context', output_channel))
        handle_values = threading.Thread(target=output_channel.pull, args=(input_channel,))
        handle_locals.start()
        handle_format_strings.start()
        handle_context.start()
        handle_values.start()
        input_globals = copy.copy(input_channel.global_namespace)
        try:
            variable_name = self.attribute.sensible_string(input_channel)
        except (StopIteration, TypeError):
            output_channel.throw('sensibleString')
        else:
            var = list(value_channel)
            for value in var:
                if isinstance(value, jqsh.values.JQSHException):
                    output_channel.throw(value)
                    break
            else:
                input_globals[variable_name] = var
        output_channel.global_namespace = input_globals
        output_channel.terminate()
        handle_locals.join()
        handle_format_strings.join()
        handle_context.join()
        handle_values.join()
    
    def run_raw(self, input_channel, output_channel):
        handle_namespaces = threading.Thread(target=input_channel.push_namespaces, args=(output_channel,))
        handle_namespaces.start()
        try:
            variable_name = self.attribute.sensible_string(input_channel)
        except (StopIteration, TypeError):
            output_channel.throw('sensibleString')
        else:
            if variable_name in input_channel.global_namespace:
                for value in input_channel.global_namespace[variable_name]:
                    output_channel.push(value)
            else:
                output_channel.throw(jqsh.values.JQSHException('name', missing_name=variable_name))
        output_channel.terminate()
        handle_namespaces.join()
