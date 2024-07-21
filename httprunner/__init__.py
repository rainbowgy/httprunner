__version__ = "v4.3.5"
__description__ = "One-stop solution for HTTP(S) testing."


from httprunner.config import Config
from httprunner.parser import parse_parameters as Parameters
from httprunner.runner import HttpRunner
from httprunner.step import Step
from httprunner.step_request import RunRequest
from httprunner.step_sql_request import (
    RunSqlRequest,
    StepSqlRequestExtraction,
    StepSqlRequestValidation,
)
from httprunner.step_testcase import RunTestCase
from httprunner.step_thrift_request import (
    RunThriftRequest,
    StepThriftRequestExtraction,
    StepThriftRequestValidation,
)

'''
用于指定在使用 from package import * 导入时应该导出的符号（类、函数、变量等）。
在这里，它指定了导出 __version__、Config 和 Parameters 这三个符号。
这样做是为了限制导出的符号，避免导入模块时导入过多不需要的内容。
'''
__all__ = [
    "__version__",
    "__description__",
    "HttpRunner",
    "Config",
    "Step",
    "RunRequest",
    "RunSqlRequest",
    "StepSqlRequestValidation",
    "StepSqlRequestExtraction",
    "RunTestCase",
    "Parameters",
    "RunThriftRequest",
    "StepThriftRequestValidation",
    "StepThriftRequestExtraction",
]
