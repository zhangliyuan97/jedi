"""
This is the core part of jedi. Most of the logic, how to evaluate certain
objects (imports, etc.) is here.

The functions should be described in their docstrings. However, there are some
classes, which are used to store the values. After those classes, there are the
search functions `get_names_for_scope` and `get_scopes_for_name`. At the end
there are the `follow_` functions, which evaluate a statement, or parts of a
statement.

TODO magic methods: __mul__, __add__, etc.
TODO evaluate asserts/isinstance (type safety)

TODO nonlocal statement, needed or can be ignored? (py3k)

TODO __ instance attributes should not be visible outside of the class.
TODO getattr / __getattr__ / __getattribute__ ?
"""
from _compatibility import next, property, hasattr, is_py3k, use_metaclass

import sys
import itertools
import copy
import weakref

import parsing
import debug
import builtin
import imports
import helpers
import dynamic

memoize_caches = []
statement_path = []
faked_scopes = []


class DecoratorNotFound(LookupError):
    """
    Decorators are sometimes not found, if that happens, that error is raised.
    """
    pass


class MultiLevelStopIteration(Exception):
    """
    StopIteration's get catched pretty easy by for loops, let errors propagate.
    """
    pass


class MultiLevelAttributeError(Exception):
    """
    Important, because `__getattr__` and `hasattr` catch AttributeErrors
    implicitly. This is really evil (mainly because of `__getattr__`).
    `hasattr` in Python 2 is even more evil, because it catches ALL exceptions.
    Therefore this class has to be a `BaseException` and not an `Exception`.
    But because I rewrote hasattr, we can now switch back to `Exception`.

    :param base: return values of sys.exc_info().
    """
    def __init__(self, base):
        self.base = base

    def __str__(self):
        import traceback
        tb = traceback.format_exception(*self.base)
        return 'Original:\n\n' + ''.join(tb)


def clear_caches():
    """
    Clears all caches of this and related modules. Jedi caches many things,
    that should be completed after each completion finishes. The only things
    that stays is the module cache (which is not deleted here).
    """
    global memoize_caches, statement_path, faked_scopes

    for m in memoize_caches:
        m.clear()

    dynamic.search_param_cache.clear()
    helpers.ExecutionRecursionDecorator.reset()

    # memorize_caches must never be deleted, because the dicts will get lost in
    # the wrappers.
    statement_path = []
    faked_scopes = []

    follow_statement.reset()


def statement_path_check(function):
    def wrapper(stmt, *args, **kwargs):
        statement_path.append(stmt)
        return function(stmt, *args, **kwargs)
    return wrapper


def memoize_default(default=None):
    """
    This is a typical memoization decorator, BUT there is one difference:
    To prevent recursion it sets defaults.

    Preventing recursion is in this case the much bigger use than speed. I
    don't think, that there is a big speed difference, but there are many cases
    where recursion could happen (think about a = b; b = a).
    """
    def func(function):
        memo = {}
        memoize_caches.append(memo)

        def wrapper(*args, **kwargs):
            key = (args, frozenset(kwargs.items()))
            if key in memo:
                return memo[key]
            else:
                memo[key] = default
                rv = function(*args, **kwargs)
                memo[key] = rv
                return rv
        return wrapper
    return func


class CachedMetaClass(type):
    """
    This is basically almost the same than the decorator above, it just caches
    class initializations. I haven't found any other way, so I do it with meta
    classes.
    """
    @memoize_default()
    def __call__(self, *args, **kwargs):
        return super(CachedMetaClass, self).__call__(*args, **kwargs)


class Executable(parsing.Base):
    """ An instance is also an executable - because __init__ is called """
    def __init__(self, base, var_args=parsing.Array(None, None)):
        self.base = base
        # The param input array.
        self.var_args = var_args

    def get_parent_until(self, *args, **kwargs):
        return self.base.get_parent_until(*args, **kwargs)

    def parent(self):
        return self.base.parent()


class Instance(use_metaclass(CachedMetaClass, Executable)):
    """ This class is used to evaluate instances. """
    def __init__(self, base, var_args=parsing.Array(None, None)):
        super(Instance, self).__init__(base, var_args)
        if str(base.name) in ['list', 'set'] \
                    and builtin.builtin_scope == base.get_parent_until():
            # compare the module path with the builtin name.
            self.var_args = dynamic.check_array_instances(self)
        else:
            # need to execute the __init__ function, because the dynamic param
            # searching needs it.
            try:
                self.execute_subscope_by_name('__init__', self.var_args)
            except KeyError:
                pass
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.is_generated = False

    @memoize_default()
    def get_init_execution(self, func):
        func = InstanceElement(self, func, True)
        return Execution(func, self.var_args)

    def get_func_self_name(self, func):
        """
        Returns the name of the first param in a class method (which is
        normally self
        """
        try:
            return func.params[0].used_vars[0].names[0]
        except IndexError:
            return None

    def get_self_properties(self):
        def add_self_dot_name(name):
            n = copy.copy(name)
            n.names = n.names[1:]
            names.append(InstanceElement(self, n))

        names = []
        # This loop adds the names of the self object, copies them and removes
        # the self.
        for sub in self.base.subscopes:
            if isinstance(sub, parsing.Class):
                continue
            # Get the self name, if there's one.
            self_name = self.get_func_self_name(sub)
            if self_name:
                # Check the __init__ function.
                if sub.name.get_code() == '__init__':
                    sub = self.get_init_execution(sub)
                for n in sub.get_set_vars():
                    # Only names with the selfname are being added.
                    # It is also important, that they have a len() of 2,
                    # because otherwise, they are just something else
                    if n.names[0] == self_name and len(n.names) == 2:
                        add_self_dot_name(n)

        for s in self.base.get_super_classes():
            if s == self.base:
                # I don't know how this could happen... But saw it once.
                continue
            names += Instance(s).get_self_properties()

        return names

    def get_subscope_by_name(self, name):
        sub = self.base.get_subscope_by_name(name)
        return InstanceElement(self, sub, True)

    def execute_subscope_by_name(self, name, args=None):
        if args is None:
            args = helpers.generate_param_array([])
        method = self.get_subscope_by_name(name)
        if args.parent_stmt() is None:
            args.parent_stmt = weakref.ref(method)
        return Execution(method, args).get_return_types()

    def get_descriptor_return(self, obj):
        """ Throws a KeyError if there's no method. """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        v = [obj, obj.base] if isinstance(obj, Instance) else [None, obj]
        args = helpers.generate_param_array(v)
        return self.execute_subscope_by_name('__get__', args)

    @memoize_default([])
    def get_defined_names(self):
        """
        Get the instance vars of a class. This includes the vars of all
        classes
        """
        names = self.get_self_properties()

        class_names = self.base.get_defined_names()
        for var in class_names:
            names.append(InstanceElement(self, var, True))
        return names

    def scope_generator(self):
        """
        An Instance has two scopes: The scope with self names and the class
        scope. Instance variables have priority over the class scope.
        """
        yield self, self.get_self_properties()

        names = []
        class_names = self.base.get_defined_names()
        for var in class_names:
            names.append(InstanceElement(self, var, True))
        yield self, names

    def get_index_types(self, index=None):
        args = helpers.generate_param_array([] if index is None else [index])
        try:
            return self.execute_subscope_by_name('__getitem__', args)
        except KeyError:
            debug.warning('No __getitem__, cannot access the array.')
            return []

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'name', 'get_imports',
                                                                'docstr']:
            raise AttributeError("Instance %s: Don't touch this (%s)!"
                                    % (self, name))
        return getattr(self.base, name)

    def __repr__(self):
        return "<e%s of %s (var_args: %s)>" % \
                (self.__class__.__name__, self.base, len(self.var_args or []))


class InstanceElement(use_metaclass(CachedMetaClass)):
    """
    InstanceElement is a wrapper for any object, that is used as an instance
    variable (e.g. self.variable or class methods).
    """
    def __init__(self, instance, var, is_class_var=False):
        if isinstance(var, parsing.Function):
            var = Function(var)
        elif isinstance(var, parsing.Class):
            var = Class(var)
        self.instance = instance
        self.var = var
        self.is_class_var = is_class_var

    @memoize_default()
    def parent(self):
        par = self.var.parent()
        if isinstance(par, Class) and par == self.instance.base \
                        or isinstance(par, parsing.Class) \
                            and par == self.instance.base.base:
            par = self.instance
        elif not isinstance(par, parsing.Module):
            par = InstanceElement(self.instance, par, self.is_class_var)
        return par

    def get_parent_until(self, *args, **kwargs):
        return parsing.Simple.get_parent_until(self, *args, **kwargs)

    def get_decorated_func(self):
        """ Needed because the InstanceElement should not be stripped """
        func = self.var.get_decorated_func()
        if func == self.var:
            return self
        return func

    def get_assignment_calls(self):
        # Copy and modify the array.
        origin = self.var.get_assignment_calls()
        # Delete parent, because it isn't used anymore.
        new = helpers.fast_parent_copy(origin)
        par = InstanceElement(self.instance, origin.parent_stmt(),
                                                    self.is_class_var)
        new.parent_stmt = weakref.ref(par)
        faked_scopes.append(par)
        faked_scopes.append(new)
        return new

    def __getattr__(self, name):
        return getattr(self.var, name)

    def isinstance(self, *cls):
        return isinstance(self.var, cls)

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.var)


class Class(use_metaclass(CachedMetaClass, parsing.Base)):
    """
    This class is not only important to extend `parsing.Class`, it is also a
    important for descriptors (if the descriptor methods are evaluated or not).
    """
    def __init__(self, base):
        self.base = base

    @memoize_default(default=[])
    def get_super_classes(self):
        supers = []
        # TODO care for mro stuff (multiple super classes).
        for s in self.base.supers:
            # Super classes are statements.
            for cls in follow_statement(s):
                if not isinstance(cls, Class):
                    debug.warning('Received non class, as a super class')
                    continue  # Just ignore other stuff (user input error).
                supers.append(cls)
        return supers

    @memoize_default(default=[])
    def get_defined_names(self):
        def in_iterable(name, iterable):
            """ checks if the name is in the variable 'iterable'. """
            for i in iterable:
                # Only the last name is important, because these names have a
                # maximal length of 2, with the first one being `self`.
                if i.names[-1] == name.names[-1]:
                    return True
            return False

        result = self.base.get_defined_names()
        super_result = []
        # TODO mro!
        for cls in self.get_super_classes():
            # Get the inherited names.
            for i in cls.get_defined_names():
                if not in_iterable(i, result):
                    super_result.append(i)
        result += super_result
        return result

    def get_subscope_by_name(self, name):
        for sub in reversed(self.subscopes):
            if sub.name.get_code() == name:
                return sub
        raise KeyError("Couldn't find subscope.")

    @property
    def name(self):
        return self.base.name

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'parent', 'subscopes',
                            'get_imports', 'get_parent_until', 'docstr']:
            raise AttributeError("Don't touch this (%s)!" % name)
        return getattr(self.base, name)

    def __repr__(self):
        return "<e%s of %s>" % (self.__class__.__name__, self.base)


class Function(use_metaclass(CachedMetaClass, parsing.Base)):
    """
    Needed because of decorators. Decorators are evaluated here.
    """

    def __init__(self, func, is_decorated=False):
        """ This should not be called directly """
        self.base_func = func
        self.is_decorated = is_decorated

    @property
    @memoize_default()
    def _decorated_func(self):
        """
        Returns the function, that is to be executed in the end.
        This is also the places where the decorators are processed.
        """
        f = self.base_func

        # Only enter it, if has not already been processed.
        if not self.is_decorated:
            for dec in reversed(self.base_func.decorators):
                debug.dbg('decorator:', dec, f)
                dec_results = follow_statement(dec)
                if not len(dec_results):
                    debug.warning('decorator func not found: %s in stmt %s' %
                                                        (self.base_func, dec))
                    return None
                if len(dec_results) > 1:
                    debug.warning('multiple decorators found', self.base_func,
                                                            dec_results)
                decorator = dec_results.pop()
                # Create param array.
                old_func = Function(f, is_decorated=True)
                params = helpers.generate_param_array([old_func], old_func)
                faked_scopes.append(old_func)

                wrappers = Execution(decorator, params).get_return_types()
                if not len(wrappers):
                    debug.warning('no wrappers found', self.base_func)
                    return None
                if len(wrappers) > 1:
                    debug.warning('multiple wrappers found', self.base_func,
                                                                wrappers)
                # This is here, that the wrapper gets executed.
                f = wrappers[0]

                debug.dbg('decorator end', f)
        if f != self.base_func and isinstance(f, parsing.Function):
            f = Function(f)
        return f

    def get_decorated_func(self):
        if self._decorated_func is None:
            raise DecoratorNotFound()
        if self._decorated_func == self.base_func:
            return self
        return self._decorated_func

    def __getattr__(self, name):
        return getattr(self.base_func, name)

    def __repr__(self):
        dec = ''
        if self._decorated_func != self.base_func:
            dec = " is " + repr(self._decorated_func)
        return "<e%s of %s%s>" % (self.__class__.__name__, self.base_func, dec)


class Execution(Executable):
    """
    This class is used to evaluate functions and their returns.

    This is the most complicated class, because it contains the logic to
    transfer parameters. It is even more complicated, because there may be
    multiple calls to functions and recursion has to be avoided. But this is
    responsibility of the decorators.
    """
    @memoize_default(default=[])
    @helpers.ExecutionRecursionDecorator
    def get_return_types(self, evaluate_generator=False):
        """ Get the return types of a function. """
        stmts = []
        if self.base.parent() == builtin.builtin_scope \
                and not isinstance(self.base, (Generator, Array)):
            func_name = str(self.base.name)

            # some implementations of builtins:
            if func_name == 'getattr':
                # follow the first param
                try:
                    objects = follow_call_list([self.var_args[0]])
                    names = follow_call_list([self.var_args[1]])
                except IndexError:
                    debug.warning('getattr() called with to few args.')
                    return []

                for obj in objects:
                    if not isinstance(obj, (Instance, Class)):
                        debug.warning('getattr called without instance')
                        return []

                    for name in names:
                        key = name.var_args.get_only_subelement()
                        try:
                            stmts.append(obj.get_subscope_by_name(key))
                        except KeyError:
                            debug.warning('called getattr() without string')
                #if not (isinstance(name, Instance) \
                        #and name.var_args:
                    #debug.warning('getattr called without instance')
                    #return []
                return stmts

        if self.base.isinstance(Class):
            # There maybe executions of executions.
            stmts = [Instance(self.base, self.var_args)]
        elif isinstance(self.base, Generator):
            return self.base.iter_content()
        else:
            # Don't do this with exceptions, as usual, because some deeper
            # exceptions could be catched - and I wouldn't know what happened.
            try:
                self.base.returns
            except (AttributeError, DecoratorNotFound):
                if hasattr(self.base, 'execute_subscope_by_name'):
                    try:
                        stmts = self.base.execute_subscope_by_name('__call__',
                                                                self.var_args)
                    except KeyError:
                        debug.warning("no __call__ func available", self.base)
                else:
                    debug.warning("no execution possible", self.base)
            else:
                stmts = self._get_function_returns(evaluate_generator)

        debug.dbg('exec result: %s in %s' % (stmts, self))

        return imports.strip_imports(stmts)

    def _get_function_returns(self, evaluate_generator):
        """ A normal Function execution """
        # Feed the listeners, with the params.
        for listener in self.base.listeners:
            listener.execute(self.get_params())
        func = self.base.get_decorated_func()
        if func.is_generator and not evaluate_generator:
            return [Generator(func, self.var_args)]
        else:
            stmts = []
            for r in self.returns:
                stmts += follow_statement(r)
            return stmts

    @memoize_default(default=[])
    def get_params(self):
        """
        This returns the params for an Execution/Instance and is injected as a
        'hack' into the parsing.Function class.
        This needs to be here, because Instance can have __init__ functions,
        which act the same way as normal functions.
        """
        def gen_param_name_copy(param, keys=[], values=[], array_type=None):
            """
            Create a param with the original scope (of varargs) as parent.
            """
            parent_stmt = self.var_args.parent_stmt()
            pos = parent_stmt.start_pos if parent_stmt else None
            calls = parsing.Array(pos, parsing.Array.NOARRAY, parent_stmt)
            calls.values = values
            calls.keys = keys
            calls.type = array_type
            new_param = copy.copy(param)
            if parent_stmt is not None:
                new_param.parent = weakref.ref(parent_stmt)
            new_param._assignment_calls_calculated = True
            new_param._assignment_calls = calls
            new_param.is_generated = True
            name = copy.copy(param.get_name())
            name.parent = weakref.ref(new_param)
            faked_scopes.append(new_param)
            return name

        result = []
        start_offset = 0
        if isinstance(self.base, InstanceElement):
            # Care for self -> just exclude it and add the instance
            start_offset = 1
            self_name = copy.copy(self.base.params[0].get_name())
            self_name.parent = weakref.ref(self.base.instance)
            result.append(self_name)

        param_dict = {}
        for param in self.base.params:
            param_dict[str(param.get_name())] = param
        # There may be calls, which don't fit all the params, this just ignores
        # it.
        var_arg_iterator = self.get_var_args_iterator()

        non_matching_keys = []
        keys_only = False
        for param in self.base.params[start_offset:]:
            # The value and key can both be null. There, the defaults apply.
            # args / kwargs will just be empty arrays / dicts, respectively.
            # Wrong value count is just ignored. If you try to test cases which
            # are not allowed in Python, Jedi will maybe not show any
            # completions.
            key, value = next(var_arg_iterator, (None, None))
            while key:
                try:
                    key_param = param_dict[str(key)]
                except KeyError:
                    non_matching_keys.append((key, value))
                else:
                    result.append(gen_param_name_copy(key_param,
                                                        values=[value]))
                key, value = next(var_arg_iterator, (None, None))
                keys_only = True

            assignments = param.get_assignment_calls().values
            assignment = assignments[0]
            keys = []
            values = []
            array_type = None
            if assignment[0] == '*':
                # *args param
                array_type = parsing.Array.TUPLE
                if value:
                    values.append(value)
                for key, value in var_arg_iterator:
                    # Iterate until a key argument is found.
                    if key:
                        var_arg_iterator.push_back((key, value))
                        break
                    values.append(value)
            elif assignment[0] == '**':
                # **kwargs param
                array_type = parsing.Array.DICT
                if non_matching_keys:
                    keys, values = zip(*non_matching_keys)
            else:
                # normal param
                if value:
                    values = [value]
                else:
                    if param.assignment_details:
                        # No value: return the default values.
                        values = assignments
                    else:
                        # If there is no assignment detail, that means there is
                        # no assignment, just the result. Therefore nothing has
                        # to be returned.
                        values = []

            # Just ignore all the params that are without a key, after one
            # keyword argument was set.
            if not keys_only or assignment[0] == '**':
                result.append(gen_param_name_copy(param, keys=keys,
                                        values=values, array_type=array_type))
        return result

    def get_var_args_iterator(self):
        """
        Yields a key/value pair, the key is None, if its not a named arg.
        """
        def iterate():
            # `var_args` is typically an Array, and not a list.
            for var_arg in self.var_args:
                # empty var_arg
                if len(var_arg) == 0:
                    yield None, None
                # *args
                elif var_arg[0] == '*':
                    arrays = follow_call_list([var_arg[1:]])
                    for array in arrays:
                        for field in array.get_contents():
                            yield None, field
                # **kwargs
                elif var_arg[0] == '**':
                    arrays = follow_call_list([var_arg[1:]])
                    for array in arrays:
                        for key, field in array.get_contents():
                            # Take the first index.
                            if isinstance(key, parsing.Name):
                                name = key
                            else:
                                # `parsing`.[Call|Function|Class] lookup.
                                name = key[0].name
                            yield name, field
                # Normal arguments (including key arguments).
                else:
                    if len(var_arg) > 1 and var_arg[1] == '=':
                        # This is a named parameter (var_arg[0] is a Call).
                        yield var_arg[0].name, var_arg[2:]
                    else:
                        yield None, var_arg

        return iter(parsing.PushBackIterator(iterate()))

    def get_set_vars(self):
        return self.get_defined_names()

    def get_defined_names(self):
        """
        Call the default method with the own instance (self implements all
        the necessary functions). Add also the params.
        """
        return self.get_params() + parsing.Scope.get_set_vars(self)

    def copy_properties(self, prop):
        """
        Literally copies a property of a Function. Copying is very expensive,
        because it is something like `copy.deepcopy`. However, these copied
        objects can be used for the executions, as if they were in the
        execution.
        """
        # Copy all these lists into this local function.
        attr = getattr(self.base, prop)
        objects = []
        for element in attr:
            copied = helpers.fast_parent_copy(element)
            copied.parent = weakref.ref(self)
            if isinstance(copied, parsing.Function):
                copied = Function(copied)
            objects.append(copied)
            faked_scopes.append(copied)
        return objects

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'imports']:
            raise AttributeError('Tried to access %s: %s. Why?' % (name, self))
        return getattr(self.base, name)

    @property
    @memoize_default()
    def returns(self):
        return self.copy_properties('returns')

    @property
    @memoize_default()
    def statements(self):
        return self.copy_properties('statements')

    @property
    @memoize_default()
    def subscopes(self):
        return self.copy_properties('subscopes')

    def get_statement_for_position(self, pos):
        return parsing.Scope.get_statement_for_position(self, pos)

    def __repr__(self):
        return "<%s of %s>" % \
                (self.__class__.__name__, self.base)


class Generator(use_metaclass(CachedMetaClass, parsing.Base)):
    """ Cares for `yield` statements. """
    def __init__(self, func, var_args):
        super(Generator, self).__init__()
        self.func = func
        self.var_args = var_args

    def get_defined_names(self):
        """
        Returns a list of names that define a generator, which can return the
        content of a generator.
        """
        names = []
        none_pos = (0, 0)
        executes_generator = ('__next__', 'send')
        for n in ('close', 'throw') + executes_generator:
            name = parsing.Name([(n, none_pos)], none_pos, none_pos)
            if n in executes_generator:
                name.parent = weakref.ref(self)
            names.append(name)
        debug.dbg('generator names', names)
        return names

    def iter_content(self):
        """ returns the content of __iter__ """
        return Execution(self.func, self.var_args).get_return_types(True)

    def get_index_types(self, index=None):
        debug.warning('Tried to get array access on a generator', self)
        return []

    def parent(self):
        return self.func.parent()

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.func)


class Array(use_metaclass(CachedMetaClass, parsing.Base)):
    """
    Used as a mirror to parsing.Array, if needed. It defines some getter
    methods which are important in this module.
    """
    def __init__(self, array):
        self._array = array

    def get_index_types(self, index_call_list=None):
        """ Get the types of a specific index or all, if not given """
        # array slicing
        if index_call_list is not None:
            if index_call_list and [x for x in index_call_list if ':' in x]:
                return [self]

            index_possibilities = list(follow_call_list(index_call_list))
            if len(index_possibilities) == 1:
                # This is indexing only one element, with a fixed index number,
                # otherwise it just ignores the index (e.g. [1+1]).
                try:
                    # Multiple elements in the array are not wanted. var_args
                    # and get_only_subelement can raise AttributeErrors.
                    i = index_possibilities[0].var_args.get_only_subelement()
                except AttributeError:
                    pass
                else:
                    try:
                        return self.get_exact_index_types(i)
                    except (IndexError, KeyError):
                        pass

        result = list(self.follow_values(self._array.values))
        result += dynamic.check_array_additions(self)
        return set(result)

    def get_exact_index_types(self, index):
        """ Here the index is an int. Raises IndexError/KeyError """
        if self._array.type == parsing.Array.DICT:
            old_index = index
            index = None
            for i, key_elements in enumerate(self._array.keys):
                # Because we only want the key to be a string.
                if len(key_elements) == 1:
                    try:
                        str_key = key_elements.get_code()
                    except AttributeError:
                        try:
                            str_key = key_elements[0].name
                        except AttributeError:
                            str_key = None
                    if old_index == str_key:
                        index = i
                        break
            if index is None:
                raise KeyError('No key found in dictionary')
        values = [self._array[index]]
        return self.follow_values(values)

    def follow_values(self, values):
        """ helper function for the index getters """
        return follow_call_list(values)

    def get_defined_names(self):
        """
        This method generates all ArrayElements for one parsing.Array.
        It returns e.g. for a list: append, pop, ...
        """
        # `array.type` is a string with the type, e.g. 'list'.
        scope = get_scopes_for_name(builtin.builtin_scope, self._array.type)[0]
        scope = Instance(scope)
        names = scope.get_defined_names()
        return [ArrayElement(n) for n in names]

    def get_contents(self):
        return self._array

    def parent(self):
        """
        Return the builtin scope as parent, because the arrays are builtins
        """
        return builtin.builtin_scope

    def get_parent_until(self, *args, **kwargs):
        return builtin.builtin_scope

    def __getattr__(self, name):
        if name not in ['type', 'start_pos']:
            raise AttributeError('Strange access: %s.' % name)
        return getattr(self._array, name)

    def __repr__(self):
        return "<e%s of %s>" % (self.__class__.__name__, self._array)


class ArrayElement(object):
    """
    A name, e.g. `list.append`, it is used to access to original array methods.
    """
    def __init__(self, name):
        super(ArrayElement, self).__init__()
        self.name = name

    def __getattr__(self, name):
        # Set access privileges:
        if name not in ['parent', 'names', 'start_pos', 'end_pos', 'get_code']:
            raise AttributeError('Strange access: %s.' % name)
        return getattr(self.name, name)

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.name)


def get_defined_names_for_position(scope, position=None, start_scope=None):
    """
    Deletes all names that are ahead of the position, except for some special
    objects like instances, where the position doesn't matter.

    :param position: the position as a line/column tuple, default is infinity.
    """
    names = scope.get_defined_names()
    # Instances have special rules, always return all the possible completions,
    # because class variables are always valid and the `self.` variables, too.
    if (not position or isinstance(scope, (Array, Instance))
                or start_scope != scope
                and isinstance(start_scope, (parsing.Function, Execution))):
        return names
    names_new = []
    for n in names:
        if n.start_pos < position:
            names_new.append(n)
    return names_new


def get_names_for_scope(scope, position=None, star_search=True,
                                                        include_builtin=True):
    """
    Get all completions possible for the current scope.
    The star search option is only here to provide an optimization. Otherwise
    the whole thing would probably start a little recursive madness.
    """
    in_func_scope = scope
    non_flow = scope.get_parent_until(parsing.Flow, reverse=True,
                                                    include_current=True)
    while scope:
        # `parsing.Class` is used, because the parent is never `Class`.
        # Ignore the Flows, because the classes and functions care for that.
        # InstanceElement of Class is ignored, if it is not the start scope.
        if not (scope != non_flow and scope.isinstance(parsing.Class)
                    or scope.isinstance(parsing.Flow)
                    or scope.isinstance(Instance)
                        and non_flow.isinstance(Function)
                    ):
            try:
                if isinstance(scope, Instance):
                    for g in scope.scope_generator():
                        yield g
                else:
                    yield scope, get_defined_names_for_position(scope,
                                                    position, in_func_scope)
            except StopIteration:
                raise MultiLevelStopIteration('StopIteration raised somewhere')
        if scope.isinstance(parsing.ForFlow) and scope.is_list_comp:
            # is a list comprehension
            yield scope, scope.get_set_vars(is_internal_call=True)

        scope = scope.parent()
        # This is used, because subscopes (Flow scopes) would distort the
        # results.
        if scope and scope.isinstance(Function, parsing.Function, Execution):
            in_func_scope = scope

    # Add star imports.
    if star_search:
        for s in imports.remove_star_imports(non_flow.get_parent_until()):
            for g in get_names_for_scope(s, star_search=False):
                yield g

        # Add builtins to the global scope.
        if include_builtin:
            builtin_scope = builtin.builtin_scope
            yield builtin_scope, builtin_scope.get_defined_names()


def get_scopes_for_name(scope, name_str, position=None, search_global=False):
    """
    This is the search function. The most important part to debug.
    `remove_statements` and `filter_statements` really are the core part of
    this completion.

    :param position: Position of the last statement -> tuple of line, column
    :return: List of Names. Their parents are the scopes, they are defined in.
    :rtype: list
    """
    def remove_statements(result):
        """
        This is the part where statements are being stripped.

        Due to lazy evaluation, statements like a = func; b = a; b() have to be
        evaluated.
        """
        res_new = []
        for r in result:
            add = []
            if r.isinstance(parsing.Statement):
                check_instance = None
                if isinstance(r, InstanceElement) and r.is_class_var:
                    check_instance = r.instance
                    r = r.var

                # Global variables handling.
                if r.is_global():
                    for token_name in r.token_list[1:]:
                        if isinstance(token_name, parsing.Name):
                            add = get_scopes_for_name(r.parent(),
                                                            str(token_name))
                else:
                    # generated objects are used within executions, where
                    if isinstance(r, parsing.Param):
                        func = r.parent()
                        # Instances are typically faked, if the instance is not
                        # called from outside. Here we check it for __init__
                        # functions and return.
                        if isinstance(func, InstanceElement) \
                                    and func.instance.is_generated \
                                    and str(func.name) == '__init__' \
                                    and r.position_nr > 0:  # 0 would be self
                            r = func.var.params[r.position_nr]
                        if not r.is_generated:
                            statement_path.append(r)
                            res_new += dynamic.search_params(r)
                            if not r.assignment_details:
                                # this means that there are no default params,
                                # so just ignore it.
                                continue

                    scopes = follow_statement(r, seek_name=name_str)
                    add += remove_statements(scopes)

                if check_instance is not None:
                    # class renames
                    add = [InstanceElement(check_instance, a, True)
                                if isinstance(a, (Function, parsing.Function))
                                else a for a in add]
                res_new += add
            else:
                if isinstance(r, parsing.Class):
                    r = Class(r)
                elif isinstance(r, parsing.Function):
                    r = Function(r)
                if r.isinstance(Function):
                    try:
                        r = r.get_decorated_func()
                    except DecoratorNotFound:
                        continue
                res_new.append(r)
        debug.dbg('sfn remove, new: %s, old: %s' % (res_new, result))
        return res_new

    def filter_name(scope_generator):
        """
        Filters all variables of a scope (which are defined in the
        `scope_generator`), until the name fits.
        """
        def handle_for_loops(loop):
            # Take the first statement (for has always only
            # one, remember `in`). And follow it.
            result = get_iterator_types(follow_statement(loop.inits[0]))
            if len(loop.set_vars) > 1:
                var_arr = loop.set_stmt.get_assignment_calls()
                result = assign_tuples(var_arr, result, name_str)
            return result

        def process(name):
            """
            Returns the parent of a name, which means the element which stands
            behind a name.
            """
            result = []
            no_break_scope = False
            par = name.parent()

            if par.isinstance(parsing.Flow):
                if par.command == 'for':
                    result += handle_for_loops(par)
                else:
                    debug.warning('Flow: Why are you here? %s' % par.command)
            elif par.isinstance(parsing.Param) \
                    and par.parent() is not None \
                    and par.parent().parent().isinstance(parsing.Class) \
                    and par.position_nr == 0:
                # This is where self gets added - this happens at another
                # place, if the var_args are clear. But sometimes the class is
                # not known. Therefore add a new instance for self. Otherwise
                # take the existing.
                statement_path.append(par)
                if isinstance(scope, InstanceElement):
                    inst = scope.instance
                else:
                    inst = Instance(Class(par.parent().parent()))
                    inst.is_generated = True
                result.append(inst)
            elif par.isinstance(parsing.Statement):
                def is_execution(arr):
                    for a in arr:
                        a = a[0]  # rest is always empty with assignees
                        if a.isinstance(parsing.Array):
                            if is_execution(a):
                                return True
                        elif a.isinstance(parsing.Call):
                            if a.name == name and a.execution:
                                return True
                    return False

                is_exe = False
                for op, assignee in par.assignment_details:
                    is_exe |= is_execution(assignee)
                if is_exe:
                    # filter array[3] = ...
                    # TODO: check executions for dict contents
                    pass
                else:
                    details = par.assignment_details
                    if details and details[0][0] != '=':
                        no_break_scope = True

                    # TODO this makes self variables non-breakable. wanted?
                    if isinstance(name, InstanceElement) \
                                                and not name.is_class_var:
                        no_break_scope = True

                    result.append(par)
            else:
                result.append(par)
            return result, no_break_scope

        result = []
        # compare func uses the tuple of line/indent = line/column
        comparison_func = lambda name: (name.start_pos)
        for scope, name_list in scope_generator:
            break_scopes = []
            # here is the position stuff happening (sorting of variables)
            for name in sorted(name_list, key=comparison_func, reverse=True):
                p = name.parent().parent() if name.parent() else None
                if isinstance(p, InstanceElement) \
                            and isinstance(p.var, parsing.Class):
                    p = p.var
                if name_str == name.get_code() and p not in break_scopes:
                    r, no_break_scope = process(name)
                    result += r
                    # for comparison we need the raw class
                    s = scope.base if isinstance(scope, Class) else scope
                    # this means that a definition was found and is not e.g.
                    # in if/else.
                    if result and not no_break_scope:
                        if not name.parent() or p == s:
                            break
                        break_scopes.append(p)
            # if there are results, ignore the other scopes
            if result:
                break

        if not result and isinstance(scope, Instance):
            # getattr() / __getattr__ / __getattribute__
            result += check_getattr(scope, name_str)
        debug.dbg('sfn filter "%s" in %s: %s' % (name_str, scope, result))
        return result

    def descriptor_check(result):
        """ Processes descriptors """
        res_new = []
        for r in result:
            if isinstance(scope, (Instance, Class)) \
                                and hasattr(r, 'get_descriptor_return'):
                # handle descriptors
                try:
                    res_new += r.get_descriptor_return(scope)
                    continue
                except KeyError:
                    pass
            res_new.append(r)
        return res_new

    if search_global:
        scope_generator = get_names_for_scope(scope, position=position)
    else:
        if isinstance(scope, Instance):
            scope_generator = scope.scope_generator()
        else:
            if isinstance(scope, Class):
                # classes are only available directly via chaining?
                # strange stuff...
                names = scope.get_defined_names()
            else:
                names = get_defined_names_for_position(scope, position)
            scope_generator = iter([(scope, names)])

    return descriptor_check(remove_statements(filter_name(scope_generator)))


def check_getattr(inst, name_str):
    result = []
    # str is important to lose the NamePart!
    name = parsing.Call(str(name_str), parsing.Call.STRING, (0, 0), inst)
    args = helpers.generate_param_array([name])
    try:
        result = inst.execute_subscope_by_name('__getattr__', args)
    except KeyError:
        pass
    if not result:
        # this is a little bit special. `__getattribute__` is executed
        # before anything else. But: I know no use case, where this
        # could be practical and the jedi would return wrong types. If
        # you ever have something, let me know!
        try:
            result = inst.execute_subscope_by_name('__getattribute__', args)
        except KeyError:
            pass
    return result


def get_iterator_types(inputs):
    """ Returns the types of any iterator (arrays, yields, __iter__, etc). """
    iterators = []
    # Take the first statement (for has always only
    # one, remember `in`). And follow it.
    for it in inputs:
        if isinstance(it, (Generator, Array, dynamic.ArrayInstance)):
            iterators.append(it)
        else:
            if not hasattr(it, 'execute_subscope_by_name'):
                debug.warning('iterator/for loop input wrong', it)
                continue
            try:
                iterators += it.execute_subscope_by_name('__iter__')
            except KeyError:
                debug.warning('iterators: No __iter__ method found.')

    result = []
    for gen in iterators:
        if isinstance(gen, Array):
            # Array is a little bit special, since this is an internal
            # array, but there's also the list builtin, which is
            # another thing.
            result += gen.get_index_types()
        elif isinstance(gen, Instance):
            # __iter__ returned an instance.
            name = '__next__' if is_py3k else 'next'
            try:
                result += gen.execute_subscope_by_name(name)
            except KeyError:
                debug.warning('Instance has no __next__ function', gen)
        else:
            # is a generator
            result += gen.iter_content()
    return result


def assign_tuples(tup, results, seek_name):
    """
    This is a normal assignment checker. In python functions and other things
    can return tuples:
    >>> a, b = 1, ""
    >>> a, (b, c) = 1, ("", 1.0)

    Here, if seek_name is "a", the number type will be returned.
    The first part (before `=`) is the param tuples, the second one result.

    :type tup: parsing.Array
    """
    def eval_results(index):
        types = []
        for r in results:
            if hasattr(r, "get_exact_index_types"):
                try:
                    types += r.get_exact_index_types(index)
                except IndexError:
                    pass
            else:
                debug.warning("invalid tuple lookup %s of result %s in %s"
                                    % (tup, results, seek_name))

        return types

    result = []
    if tup.type == parsing.Array.NOARRAY:
        # Here we have unnessecary braces, which we just remove.
        arr = tup.get_only_subelement()
        result = assign_tuples(arr, results, seek_name)
    else:
        for i, t in enumerate(tup):
            # Used in assignments. There is just one call and no other things,
            # therefore we can just assume, that the first part is important.
            if len(t) != 1:
                raise AttributeError('Array length should be 1')
            t = t[0]

            # Check the left part, if there are still tuples in it or a Call.
            if isinstance(t, parsing.Array):
                # These are "sub"-tuples.
                result += assign_tuples(t, eval_results(i), seek_name)
            else:
                if t.name.names[-1] == seek_name:
                    result += eval_results(i)
    return result


@helpers.RecursionDecorator
@statement_path_check
@memoize_default(default=[])
def follow_statement(stmt, seek_name=None):
    """
    The starting point of the completion. A statement always owns a call list,
    which are the calls, that a statement does.
    In case multiple names are defined in the statement, `seek_name` returns
    the result for this name.

    :param stmt: A `parsing.Statement`.
    :param seek_name: A string.
    """
    debug.dbg('follow_stmt %s (%s)' % (stmt, seek_name))
    call_list = stmt.get_assignment_calls()
    debug.dbg('calls: %s' % call_list)

    try:
        result = follow_call_list(call_list)
    except AttributeError:
        # This is so evil! But necessary to propagate errors. The attribute
        # errors here must not be catched, because they shouldn't exist.
        raise MultiLevelAttributeError(sys.exc_info())

    # Assignment checking is only important if the statement defines multiple
    # variables.
    if len(stmt.get_set_vars()) > 1 and seek_name and stmt.assignment_details:
        new_result = []
        for op, set_vars in stmt.assignment_details:
            new_result += assign_tuples(set_vars, result, seek_name)
        result = new_result
    return set(result)


def follow_call_list(call_list):
    """
    The call_list has a special structure.
    This can be either `parsing.Array` or `list of list`.
    It is used to evaluate a two dimensional object, that has calls, arrays and
    operators in it.
    """
    def evaluate_list_comprehension(lc, parent=None):
        input = lc.input
        nested_lc = lc.input.token_list[0]
        if isinstance(nested_lc, parsing.ListComprehension):
            # is nested LC
            input = nested_lc.stmt
        loop = parsing.ForFlow([input], lc.stmt.start_pos,
                                                lc.middle, True)
        if parent is None:
            loop.parent = weakref.ref(lc.stmt.parent())
        else:
            loop.parent = lambda: parent

        if isinstance(nested_lc, parsing.ListComprehension):
            loop = evaluate_list_comprehension(nested_lc, loop)
        return loop

    if parsing.Array.is_type(call_list, parsing.Array.TUPLE,
                                        parsing.Array.DICT):
        # Tuples can stand just alone without any braces. These would be
        # recognized as separate calls, but actually are a tuple.
        result = follow_call(call_list)
    else:
        result = []
        for calls in call_list:
            calls_iterator = iter(calls)
            for call in calls_iterator:
                if parsing.Array.is_type(call, parsing.Array.NOARRAY):
                    result += follow_call_list(call)
                elif isinstance(call, parsing.ListComprehension):
                    loop = evaluate_list_comprehension(call)
                    stmt = copy.copy(call.stmt)
                    stmt.parent = lambda: loop
                    # create a for loop which does the same as list
                    # comprehensions
                    result += follow_statement(stmt)
                else:
                    if isinstance(call, (Function, Class, Instance,
                                            dynamic.ArrayInstance)):
                    # With things like params, these can also be functions...
                        result.append(call)
                    # The string tokens are just operations (+, -, etc.)
                    elif not isinstance(call, str):
                        if str(call.name) == 'if':
                            # Ternary operators.
                            while True:
                                try:
                                    call = next(calls_iterator)
                                except StopIteration:
                                    break
                                try:
                                    if str(call.name) == 'else':
                                        break
                                except AttributeError:
                                    pass
                            continue
                        result += follow_call(call)
    return set(result)


def follow_call(call):
    """ Follow a call is following a function, variable, string, etc. """
    scope = call.parent_stmt().parent()
    path = call.generate_call_path()
    position = call.parent_stmt().start_pos
    return follow_call_path(path, scope, position)


def follow_call_path(path, scope, position):
    """ Follows a path generated by `parsing.Call.generate_call_path()` """
    current = next(path)

    if isinstance(current, parsing.Array):
        result = [Array(current)]
    else:
        if not isinstance(current, parsing.NamePart):
            if current.type in (parsing.Call.STRING, parsing.Call.NUMBER):
                t = type(current.name).__name__
                scopes = get_scopes_for_name(builtin.builtin_scope, t)
            else:
                debug.warning('unknown type:', current.type, current)
                scopes = []
            # Make instances of those number/string objects.
            arr = helpers.generate_param_array([current.name])
            scopes = [Instance(s, arr) for s in scopes]
        else:
            # This is the first global lookup.
            scopes = get_scopes_for_name(scope, current, position=position,
                                            search_global=True)
        result = imports.strip_imports(scopes)

        if result != scopes:
            # Reset the position, when imports where stripped.
            position = None

    return follow_paths(path, result, position=position)


def follow_paths(path, results, position=None):
    """
    In each result, `path` must be followed. Copies the path iterator.
    """
    results_new = []
    if results:
        if len(results) > 1:
            iter_paths = itertools.tee(path, len(results))
        else:
            iter_paths = [path]

        for i, r in enumerate(results):
            fp = follow_path(iter_paths[i], r, position=position)
            if fp is not None:
                results_new += fp
            else:
                # This means stop iteration.
                return results
    return results_new


def follow_path(path, scope, position=None):
    """
    Uses a generator and tries to complete the path, e.g.
    >>> foo.bar.baz

    `follow_path` is only responsible for completing `.bar.baz`, the rest is
    done in the `follow_call` function.
    """
    # Current is either an Array or a Scope.
    try:
        current = next(path)
    except StopIteration:
        return None
    debug.dbg('follow %s in scope %s' % (current, scope))

    result = []
    if isinstance(current, parsing.Array):
        # This must be an execution, either () or [].
        if current.type == parsing.Array.LIST:
            result = scope.get_index_types(current)
        elif current.type not in [parsing.Array.DICT]:
            # Scope must be a class or func - make an instance or execution.
            debug.dbg('exe', scope)
            result = Execution(scope, current).get_return_types()
        else:
            # Curly braces are not allowed, because they make no sense.
            debug.warning('strange function call with {}', current, scope)
    else:
        # The function must not be decorated with something else.
        if isinstance(scope, Function):
            # TODO Check default function methods and return them.
            result = []
        else:
            # TODO Check magic class methods and return them also.
            # This is the typical lookup while chaining things.
            result = imports.strip_imports(get_scopes_for_name(scope, current,
                                                        position=position))
    return follow_paths(path, set(result), position=position)


def goto(scopes, search_name=None, statement_path_offset=1,
                                                        follow_import=False):
    def follow_imports(names):
        global statement_path
        new = []
        for n in names:
            if isinstance(n, parsing.Import):
                statement_path = []
                scopes = imports.strip_imports([n])
                new += goto(scopes, follow_import=True)
            else:
                new.append(n)
        return new

    if search_name is None:
        try:
            definitions = [statement_path[statement_path_offset]]
        except IndexError:
            definitions = []
            for s in scopes:
                if isinstance(s, imports.ImportPath):
                    try:
                        s = s.follow()[0]
                    except IndexError:
                        continue
                    else:
                        if not isinstance(s, parsing.Module):
                            s = statement_path[0]
                definitions.append(s)
    else:
        names = []
        for s in scopes:
            if isinstance(s, imports.ImportPath):
                modules = s.follow()
                if modules:
                    names.append(modules[0].get_module_name())
            else:
                names += s.get_defined_names()
        definitions = [n for n in names if n.names[-1] == search_name]

    #if follow_import:
    #    definitions = follow_imports(definitions)

    definitions = set(definitions)
    #for d in definitions.copy():
        #if d.isinstance(Function, Class):
        #    definitions.add(d.name)
        #    definitions.remove(d)
    return definitions
