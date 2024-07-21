import ast
import builtins
import os
import re
from typing import Any, Callable, Dict, List, Set, Text
from urllib.parse import urlparse

from loguru import logger

from httprunner import exceptions, loader, utils
from httprunner.models import FunctionsMapping, VariablesMapping

# use $$ to escape $ notation
dolloar_regex_compile = re.compile(r"\$\$")
# variable notation, e.g. ${var} or $var
# variable should start with a-zA-Z_
variable_regex_compile = re.compile(r"\$\{([a-zA-Z_]\w*)\}|\$([a-zA-Z_]\w*)")
# function notation, e.g. ${func1($var_1, $var_3)}
# 两个捕获组，group[0]是([a-zA-Z_]\w*)，group[1]是([\$\w\.\-/\s=,]*)
function_regex_compile = re.compile(r"\$\{([a-zA-Z_]\w*)\(([\$\w\.\-/\s=,]*)\)\}")


def parse_string_value(str_value: Text) -> Any:
    """parse string to number if possible
    e.g. "123" => 123
         "12.2" => 12.3
         "abc" => "abc"
         "$var" => "$var"
    """
    try:
        # 不执行任何代码，安全的解析评估变量是否是python字面量，如数字、字符串、元组、列表、字典、布尔值、None 等
        return ast.literal_eval(str_value)
    except ValueError:
        return str_value
    except SyntaxError:
        # e.g. $var, ${func}
        return str_value


def build_url(base_url, step_url):
    """prepend url with base_url unless it's already an absolute URL"""
    o_step_url = urlparse(step_url)
    if o_step_url.netloc != "":
        # step url is absolute url
        return step_url

    # step url is relative, based on base url
    o_base_url = urlparse(base_url)
    if o_base_url.netloc == "":
        # missed base url
        raise exceptions.ParamsError("base url missed!")

    path = o_base_url.path.rstrip("/") + "/" + o_step_url.path.lstrip("/")
    o_step_url = (
        o_step_url._replace(scheme=o_base_url.scheme)
        ._replace(netloc=o_base_url.netloc)
        ._replace(path=path)
    )
    return o_step_url.geturl()


def regex_findall_variables(raw_string: Text) -> List[Text]:
    """extract all variable names from content, which is in format $variable

    Args:
        raw_string (str): string content

    Returns:
        list: variables list extracted from string content

    Examples:
        >>> regex_findall_variables("$variable")
        ["variable"]

        >>> regex_findall_variables("/blog/$postid")
        ["postid"]

        >>> regex_findall_variables("/$var1/$var2")
        ["var1", "var2"]

        >>> regex_findall_variables("abc")
        []

    """
    try:
        # 从开始的位置找到第一个$,返回索引
        match_start_position = raw_string.index("$", 0)
    except ValueError:
        return []

    vars_list = []
    while match_start_position < len(raw_string):

        # Notice: notation priority
        # $$ > $var

        # search $$，$$value就不算，如果想要算这段代码就可以只是掉
        # match只从指定的开始位置寻找是否匹配，开始位置没有则返回none
        dollar_match = dolloar_regex_compile.match(raw_string, match_start_position)
        if dollar_match:
            match_start_position = dollar_match.end()
            continue

        # search variable like ${var} or $var
        var_match = variable_regex_compile.match(raw_string, match_start_position)
        if var_match:
            # "\$\{([a-zA-Z_]\w*)\}|\$([a-zA-Z_]\w*)"里两个（）就是两个匹配组，第一组是${var1},第二组是$var2
            # var_match.group(1)}=var1,var_match.group(2)=var2,同时肯定只符合一个，另一个是none
            var_name = var_match.group(1) or var_match.group(2)
            vars_list.append(var_name)
            # print(f'匹配分组1:{var_match.group(1)}')
            # print(f'匹配分组2:{var_match.group(2)}')
            match_start_position = var_match.end()
            continue

        curr_position = match_start_position
        try:
            # find next $ location
            match_start_position = raw_string.index("$", curr_position + 1)
        except ValueError:
            # break while loop
            break

    return vars_list


def regex_findall_functions(content: Text) -> List[Text]:
    """extract all functions from string content, which are in format ${fun()}

    Args:
        content (str): string content

    Returns:
        list: functions list extracted from string content

    Examples:
        >>> regex_findall_functions("${func(5)}")
        ["func(5)"]

        >>> regex_findall_functions("${func(a=1, b=2)}")
        ["func(a=1, b=2)"]

        >>> regex_findall_functions("/api/1000?_t=${get_timestamp()}")
        ["get_timestamp()"]

        >>> regex_findall_functions("/api/${add(1, 2)}")
        ["add(1, 2)"]

        >>> regex_findall_functions("/api/${add(1, 2)}?_t=${get_timestamp()}")
        ["add(1, 2)", "get_timestamp()"]

    """
    try:
        return function_regex_compile.findall(content)
    except TypeError as ex:
        logger.error(f"regex findall functions error: {ex}")
        return []


def extract_variables(content: Any) -> Set:
    """extract all variables in content recursively."""
    if isinstance(content, (list, set, tuple)):
        variables = set()
        for item in content:
            variables = variables | extract_variables(item)
        return variables

    elif isinstance(content, dict):
        variables = set()
        for key, value in content.items():
            variables = variables | extract_variables(value)
        return variables

    elif isinstance(content, str):
        return set(regex_findall_variables(content))

    return set()


def parse_function_params(params: Text) -> Dict:
    """parse function params to args and kwargs.

    Args:
        params (str): function param in string

    Returns:
        dict: function meta dict

            {
                "args": [],
                "kwargs": {}
            }

    Examples:
        >>> parse_function_params("")
        {'args': [], 'kwargs': {}}

        >>> parse_function_params("5")
        {'args': [5], 'kwargs': {}}

        >>> parse_function_params("1, 2")
        {'args': [1, 2], 'kwargs': {}}

        >>> parse_function_params("a=1, b=2")
        {'args': [], 'kwargs': {'a': 1, 'b': 2}}

        >>> parse_function_params("1, 2, a=3, b=4")
        {'args': [1, 2], 'kwargs': {'a':3, 'b':4}}

    """
    function_meta = {"args": [], "kwargs": {}}

    # 去除首位空白字符，比如空格/制表符/换行符
    params_str = params.strip()
    if params_str == "":
        return function_meta

    args_list = params_str.split(",")
    for arg in args_list:
        arg = arg.strip()
        if "=" in arg:
            key, value = arg.split("=")
            function_meta["kwargs"][key.strip()] = parse_string_value(value.strip())
        else:
            function_meta["args"].append(parse_string_value(arg))

    return function_meta


def get_mapping_variable(
    variable_name: Text, variables_mapping: VariablesMapping
) -> Any:
    """get variable from variables_mapping.

    Args:
        variable_name (str): variable name
        variables_mapping (dict): variables mapping

    Returns:
        mapping variable value.

    Raises:
        exceptions.VariableNotFound: variable is not found.

    """
    # TODO: get variable from debugtalk module and environ
    try:
        return variables_mapping[variable_name]
    except KeyError:
        raise exceptions.VariableNotFound(
            f"{variable_name} not found in {variables_mapping}"
        )


def get_mapping_function(
    function_name: Text, functions_mapping: FunctionsMapping
) -> Callable:
    """get function from functions_mapping,
        if not found, then try to check if builtin function.

    Args:
        function_name (str): function name
        functions_mapping (dict): functions mapping

    Returns:
        mapping function object.

    Raises:
        exceptions.FunctionNotFound: function is neither defined in debugtalk.py nor builtin.

    """
    if function_name in functions_mapping:
        # callable类型
        return functions_mapping[function_name]

    elif function_name in ["parameterize", "P"]:
        # callable类型
        return loader.load_csv_file

    elif function_name in ["environ", "ENV"]:
        # callable类型
        return utils.get_os_environ

    elif function_name in ["multipart_encoder", "multipart_content_type"]:
        # extension for upload test
        from httprunner.ext import uploader
        # getattr是从uploader对象里获取function_name属性或者方法，然后返回一个函数引用或者属性引用，是一种动态调用方法
        # 比如，uploader里定义了def upload_to_s3(file_path)，upload_function = getattr(uploader, 'upload_to_s3')
        # 调用函数，upload_function('/path/to/your/file.txt')
        return getattr(uploader, function_name)

    try:
        # check if HttpRunner builtin functions
        # 返回httprunner.builtin包下的所有模块的函数，这里是comparators,functions
        # 感觉这里是不是也可以直接使用上面uploader的方式
        built_in_functions = loader.load_builtin_functions()
        return built_in_functions[function_name]
    except KeyError:
        pass

    try:
        # check if Python builtin functions
        return getattr(builtins, function_name)
    except AttributeError:
        pass

    raise exceptions.FunctionNotFound(f"{function_name} is not found.")


def parse_string(
    raw_string: Text,
    variables_mapping: VariablesMapping,
    functions_mapping: FunctionsMapping,
) -> Any:
    """parse string content with variables and functions mapping.

    Args:
        raw_string: raw string content to be parsed.
        variables_mapping: variables mapping.
        functions_mapping: functions mapping.

    Returns:
        str: parsed string content.

    Examples:
        >>> raw_string = "abc${add_one($num)}def"
        >>> variables_mapping = {"num": 3}
        >>> functions_mapping = {"add_one": lambda x: x + 1}
        >>> parse_string(raw_string, variables_mapping, functions_mapping)
            "abc4def"

    """
    try:
        # 从字符串索引0开始查找$，返回第一次出现的位置
        match_start_position = raw_string.index("$", 0)
        # 不包含match_start_position
        parsed_string = raw_string[0:match_start_position]
    except ValueError:
        # 没出现$,则直接返回
        parsed_string = raw_string
        return parsed_string

    while match_start_position < len(raw_string):

        # Notice: notation priority
        # $$ > ${func($a, $b)} > $var

        # search $$
        dollar_match = dolloar_regex_compile.match(raw_string, match_start_position)
        if dollar_match:
            match_start_position = dollar_match.end()
            parsed_string += "$"
            continue

        # 替换可调用函数变量
        # search function like ${func($a, $b)}
        func_match = function_regex_compile.match(raw_string, match_start_position)
        if func_match:
            func_name = func_match.group(1)
            # 返回一个可调用的函数或者属性，callable类型，除了debugtalk里的还有csv，httprunner.builtin,python builtins里的函数
            func = get_mapping_function(func_name, functions_mapping)
            # 可调用函数参数
            func_params_str = func_match.group(2)
            # 把参数整理分类成列表args和字典kwargs两种
            function_meta = parse_function_params(func_params_str)
            args = function_meta["args"]
            kwargs = function_meta["kwargs"]
            # 进一步解析函数参数里的变量
            parsed_args = parse_data(args, variables_mapping, functions_mapping)
            parsed_kwargs = parse_data(kwargs, variables_mapping, functions_mapping)

            try:
                # 调用函数
                func_eval_value = func(*parsed_args, **parsed_kwargs)
            except Exception as ex:
                logger.error(
                    f"call function error:\n"
                    f"func_name: {func_name}\n"
                    f"args: {parsed_args}\n"
                    f"kwargs: {parsed_kwargs}\n"
                    f"{type(ex).__name__}: {ex}"
                )
                raise

            func_raw_str = "${" + func_name + f"({func_params_str})" + "}"
            if func_raw_str == raw_string:
                # raw_string is a function, e.g. "${add_one(3)}", return its eval value directly
                return func_eval_value

            # raw_string contains one or many functions, e.g. "abc${add_one(3)}def"
            parsed_string += str(func_eval_value)
            match_start_position = func_match.end()
            continue


        # 替换变量
        # search variable like ${var} or $var
        var_match = variable_regex_compile.match(raw_string, match_start_position)
        if var_match:
            var_name = var_match.group(1) or var_match.group(2)
            # 目前只是直接匹配variables_mapping的变量，不包含debugtalk和env里的变量，不存在则报错
            var_value = get_mapping_variable(var_name, variables_mapping)

            if f"${var_name}" == raw_string or "${" + var_name + "}" == raw_string:
                # raw_string is a variable, $var or ${var}, return its value directly
                return var_value

            # raw_string contains one or many variables, e.g. "abc${var}def"
            parsed_string += str(var_value)
            match_start_position = var_match.end()
            continue

        curr_position = match_start_position
        try:
            # find next $ location
            match_start_position = raw_string.index("$", curr_position + 1)
            remain_string = raw_string[curr_position:match_start_position]
        except ValueError:
            remain_string = raw_string[curr_position:]
            # break while loop
            match_start_position = len(raw_string)

        parsed_string += remain_string

    return parsed_string


def parse_data(
    raw_data: Any,
    variables_mapping: VariablesMapping = None,
    functions_mapping: FunctionsMapping = None,
) -> Any:
    """parse raw data with evaluated variables mapping.
    Notice: variables_mapping should not contain any variable or function.
    """
    if isinstance(raw_data, str):
        # content in string format may contains variables and functions
        variables_mapping = variables_mapping or {}
        functions_mapping = functions_mapping or {}
        # only strip whitespaces and tabs, \n\r is left because they maybe used in changeset
        raw_data = raw_data.strip(" \t")
        return parse_string(raw_data, variables_mapping, functions_mapping)

    elif isinstance(raw_data, (list, set, tuple)):
        return [
            parse_data(item, variables_mapping, functions_mapping) for item in raw_data
        ]

    elif isinstance(raw_data, dict):
        parsed_data = {}
        for key, value in raw_data.items():
            parsed_key = parse_data(key, variables_mapping, functions_mapping)
            parsed_value = parse_data(value, variables_mapping, functions_mapping)
            parsed_data[parsed_key] = parsed_value

        return parsed_data

    else:
        # other types, e.g. None, int, float, bool
        return raw_data


def parse_variables_mapping(
    variables_mapping: VariablesMapping, functions_mapping: FunctionsMapping = None
) -> VariablesMapping:

    parsed_variables: VariablesMapping = {}

    # 判断解析过的变量和要解析的变量长度是否一致，解决了$foo1里调用$foo2,但是遍历是按照顺序的，解析foo1的时候还没解析foo2的问题
    # 但是如果是互相调用，就会进入死循环，这里应该加一个循环限制
    while len(parsed_variables) != len(variables_mapping):
        for var_name in variables_mapping:

            # 如果已经解析过则跳过
            if var_name in parsed_variables:
                continue

            var_value = variables_mapping[var_name]
            # 提取变量值里使用的变量$value/${value}
            variables = extract_variables(var_value)

            # check if reference variable itself
            # 如果变量值里调用的变量是当前遍历的变量key自己，抛出异常
            if var_name in variables:
                # e.g.
                # variables_mapping = {"token": "abc$token"}
                # variables_mapping = {"key": ["$key", 2]}
                # 抛出异常，方法又没有向上抛出异常，走到这里会异常中断
                raise exceptions.VariableNotFound(var_name)

            # check if reference variable not in variables_mapping
            # 列表推导式，在变量值里提取的变量如果不在变量列表里，抛出异常
            not_defined_variables = [
                v_name for v_name in variables if v_name not in variables_mapping
            ]
            if not_defined_variables:
                # e.g. {"varA": "123$varB", "varB": "456$varC"}
                # e.g. {"varC": "${sum_two($a, $b)}"}
                raise exceptions.VariableNotFound(not_defined_variables)

            try:
                parsed_value = parse_data(
                    var_value, parsed_variables, functions_mapping
                )
            except exceptions.VariableNotFound:
                # 捕捉到可替换的变量里不存在，跳过当前循环
                continue

            parsed_variables[var_name] = parsed_value

    return parsed_variables


def parse_parameters(
    parameters: Dict,
) -> List[Dict]:
    """parse parameters and generate cartesian product.

    Args:
        parameters (Dict) parameters: parameter name and value mapping
            parameter value may be in three types:
                (1) data list, e.g. ["iOS/10.1", "iOS/10.2", "iOS/10.3"]
                (2) call built-in parameterize function, "${parameterize(account.csv)}"
                (3) call custom function in debugtalk.py, "${gen_app_version()}"

    Returns:
        list: cartesian product list

    Examples:
        >>> parameters = {
            "user_agent": ["iOS/10.1", "iOS/10.2", "iOS/10.3"],
            "username-password": "${parameterize(account.csv)}",
            "app_version": "${gen_app_version()}",
        }
        >>> parse_parameters(parameters)

    """
    parsed_parameters_list: List[List[Dict]] = []

    # load project_meta functions
    project_meta = loader.load_project_meta(os.getcwd())
    functions_mapping = project_meta.functions

    for parameter_name, parameter_content in parameters.items():
        parameter_name_list = parameter_name.split("-")

        if isinstance(parameter_content, List):
            # (1) data list
            # e.g. {"app_version": ["2.8.5", "2.8.6"]}
            #       => [{"app_version": "2.8.5", "app_version": "2.8.6"}]
            # e.g. {"username-password": [["user1", "111111"], ["test2", "222222"]}
            #       => [{"username": "user1", "password": "111111"}, {"username": "user2", "password": "222222"}]
            parameter_content_list: List[Dict] = []
            for parameter_item in parameter_content:
                if not isinstance(parameter_item, (list, tuple)):
                    # "2.8.5" => ["2.8.5"]
                    parameter_item = [parameter_item]

                # ["app_version"], ["2.8.5"] => {"app_version": "2.8.5"}
                # ["username", "password"], ["user1", "111111"] => {"username": "user1", "password": "111111"}
                parameter_content_dict = dict(zip(parameter_name_list, parameter_item))
                parameter_content_list.append(parameter_content_dict)

        elif isinstance(parameter_content, Text):
            # (2) & (3)
            parsed_parameter_content: List = parse_data(
                parameter_content, {}, functions_mapping
            )
            if not isinstance(parsed_parameter_content, List):
                raise exceptions.ParamsError(
                    f"parameters content should be in List type, got {parsed_parameter_content} for {parameter_content}"
                )

            parameter_content_list: List[Dict] = []
            for parameter_item in parsed_parameter_content:
                if isinstance(parameter_item, Dict):
                    # get subset by parameter name
                    # {"app_version": "${gen_app_version()}"}
                    # gen_app_version() => [{'app_version': '2.8.5'}, {'app_version': '2.8.6'}]
                    # {"username-password": "${get_account()}"}
                    # get_account() => [
                    #       {"username": "user1", "password": "111111"},
                    #       {"username": "user2", "password": "222222"}
                    # ]
                    parameter_dict: Dict = {
                        key: parameter_item[key] for key in parameter_name_list
                    }
                elif isinstance(parameter_item, (List, tuple)):
                    if len(parameter_name_list) == len(parameter_item):
                        # {"username-password": "${get_account()}"}
                        # get_account() => [("user1", "111111"), ("user2", "222222")]
                        parameter_dict = dict(zip(parameter_name_list, parameter_item))
                    else:
                        raise exceptions.ParamsError(
                            f"parameter names length are not equal to value length.\n"
                            f"parameter names: {parameter_name_list}\n"
                            f"parameter values: {parameter_item}"
                        )
                elif len(parameter_name_list) == 1:
                    # {"user_agent": "${get_user_agent()}"}
                    # get_user_agent() => ["iOS/10.1", "iOS/10.2"]
                    # parameter_dict will get: {"user_agent": "iOS/10.1", "user_agent": "iOS/10.2"}
                    parameter_dict = {parameter_name_list[0]: parameter_item}
                else:
                    raise exceptions.ParamsError(
                        f"Invalid parameter names and values:\n"
                        f"parameter names: {parameter_name_list}\n"
                        f"parameter values: {parameter_item}"
                    )

                parameter_content_list.append(parameter_dict)

        else:
            raise exceptions.ParamsError(
                f"parameter content should be List or Text(variables or functions call), got {parameter_content}"
            )

        parsed_parameters_list.append(parameter_content_list)

    return utils.gen_cartesian_product(*parsed_parameters_list)


class Parser(object):
    def __init__(self, functions_mapping: FunctionsMapping = None) -> None:
        self.functions_mapping = functions_mapping

    def parse_string(
        self, raw_string: Text, variables_mapping: VariablesMapping
    ) -> Any:
        return parse_string(raw_string, variables_mapping, self.functions_mapping)

    def parse_variables(self, variables_mapping: VariablesMapping) -> VariablesMapping:
        return parse_variables_mapping(variables_mapping, self.functions_mapping)

    def parse_data(
        self, raw_data: Any, variables_mapping: VariablesMapping = None
    ) -> Any:
        return parse_data(raw_data, variables_mapping, self.functions_mapping)

    def get_mapping_function(self, func_name: Text) -> Callable:
        return get_mapping_function(func_name, self.functions_mapping)
