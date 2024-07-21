import os
import time
import uuid
from datetime import datetime
from typing import Dict, List, Text

try:
    import allure

    ALLURE = allure
except ModuleNotFoundError:
    ALLURE = None

from loguru import logger

from httprunner.client import HttpSession
from httprunner.config import Config
from httprunner.exceptions import ParamsError, ValidationFailure
from httprunner.loader import load_project_meta
from httprunner.models import (
    ProjectMeta,
    StepResult,
    TConfig,
    TestCaseInOut,
    TestCaseSummary,
    TestCaseTime,
    VariablesMapping,
)
from httprunner.parser import Parser
from httprunner.utils import LOGGER_FORMAT, merge_variables, ga4_client


class SessionRunner(object):
    # 用例文件在调用test_start()方法时，测试类的属性config和teststeps会覆盖这里的类属性，所以这里不需要初始化值
    config: Config
    teststeps: List[object]  # list of Step

    parser: Parser = None
    session: HttpSession = None
    case_id: Text = ""
    root_dir: Text = ""
    thrift_client = None
    db_engine = None

    __config: TConfig
    __project_meta: ProjectMeta = None
    __export: List[Text] = []
    __step_results: List[StepResult] = []
    __session_variables: VariablesMapping = {}
    __is_referenced: bool = False
    # time
    __start_at: float = 0
    __duration: float = 0
    # log
    __log_path: Text = ""

    # 注意这里不是__init__，所以代码没报错
    def __init(self):
        # self.config不另外赋值的情况下，会读取类属性config的值,而这个值又被测试文件的config实例覆盖
        # struct()会返回self.__config,里面包含self.__config.name，self.__config.url/variables等
        self.__config = self.config.struct()
        self.__session_variables = self.__session_variables or {}
        self.__start_at = 0
        self.__duration = 0
        self.__is_referenced = self.__is_referenced or False

        # 返回一个ProjectMeta模型，包含debugtalk_path/functions/RootDir/env/dot_env_path
        # 分别代表debugtalk.py文件路径，文件里的fuction（动态导入过），文件所在目录，目录下的.env键值对，和路径
        self.__project_meta = self.__project_meta or load_project_meta(
            self.__config.path  #  还记得吗，这里的path是用例脚本py文件绝对路径
            # '/Users/guoyan/work/pythonProject/httprunner/examples/postman_echo/request_methods/request_with_variables_test.py'
        )
        # 生成随机 UUID，唯一标识符
        self.case_id = self.case_id or str(uuid.uuid4())
        self.root_dir = self.root_dir or self.__project_meta.RootDir
        self.__log_path = os.path.join(self.root_dir, "logs", f"{self.case_id}.run.log")

        self.__step_results = self.__step_results or []
        # 继承requests.Session，初始化SessionData模型，including request, response, validators and stat data
        self.session = self.session or HttpSession()
        # 把function_mapping赋值给self.parser实例
        self.parser = self.parser or Parser(self.__project_meta.functions)

    def with_session(self, session: HttpSession) -> "SessionRunner":
        self.session = session
        return self

    def get_config(self) -> TConfig:
        return self.__config

    def set_referenced(self) -> "SessionRunner":
        self.__is_referenced = True
        return self

    def with_case_id(self, case_id: Text) -> "SessionRunner":
        self.case_id = case_id
        return self

    def with_variables(self, variables: VariablesMapping) -> "SessionRunner":
        self.__session_variables = variables
        return self

    def with_export(self, export: List[Text]) -> "SessionRunner":
        # RunTestCase.export
        self.__export = export
        return self

    def with_thrift_client(self, thrift_client) -> "SessionRunner":
        self.thrift_client = thrift_client
        return self

    def with_db_engine(self, db_engine) -> "SessionRunner":
        self.db_engine = db_engine
        return self

    def __parse_config(self, param: Dict = None) -> None:
        # parse config variables，前面step提取的变量更新到config里，或者用例1调用用例2时，执行用例2，会把用例1的config和step变量
        # 更新在这个session里带进来，session的优先级更大
        self.__config.variables.update(self.__session_variables)
        if param:
            self.__config.variables.update(param)
            # parse_variables_mapping里捕捉了异常
        self.__config.variables = self.parser.parse_variables(self.__config.variables)

        # parse config name
        # 要替换的变量在config.variable里没有，会抛出异常，这里没有捕捉异常会直接中断程序报错
        self.__config.name = self.parser.parse_data(
            self.__config.name, self.__config.variables
        )

        # parse config base url
        self.__config.base_url = self.parser.parse_data(
            self.__config.base_url, self.__config.variables
        )

    def get_export_variables(self) -> Dict:
        # override testcase export vars with step export
        # self.__export是testcase_step里的export
        export_var_names = self.__export or self.__config.export
        export_vars_mapping = {}
        for var_name in export_var_names:
            # 从__session_variables里取值导出，__session_variables包含step提取的变量
            # 还有用例1调用用例2时，用例1定义的step和config变量
            if var_name not in self.__session_variables:
                raise ParamsError(
                    f"failed to export variable {var_name} from session variables {self.__session_variables}"
                )

            export_vars_mapping[var_name] = self.__session_variables[var_name]

        return export_vars_mapping

    def get_summary(self) -> TestCaseSummary:
        """get testcase result summary"""
        start_at_timestamp = self.__start_at
        start_at_iso_format = datetime.utcfromtimestamp(start_at_timestamp).isoformat()

        summary_success = True
        for step_result in self.__step_results:
            if not step_result.success:
                summary_success = False
                break

        return TestCaseSummary(
            name=self.__config.name,
            success=summary_success,
            case_id=self.case_id,
            time=TestCaseTime(
                start_at=self.__start_at,
                start_at_iso_format=start_at_iso_format,
                duration=self.__duration,
            ),
            in_out=TestCaseInOut(
                config_vars=self.__config.variables,
                export_vars=self.get_export_variables(),
            ),
            log=self.__log_path,
            step_results=self.__step_results,
        )

    def merge_step_variables(self, variables: VariablesMapping) -> VariablesMapping:
        # override variables
        # step variables > extracted variables from previous steps包括用例里调用另一个用例
        # 还包括用例里调用另一个用例，会把当前用例的config变量和step变量传递给另一个用例
        # 用例A调用用例B，用例B step里的变量>用例A过来的变量>用例B的config变量
        # __session_variables指上一步关联过来的变量，上一步可以是testcase也可以是上一个响应提取的变量
        variables = merge_variables(variables, self.__session_variables)
        # step variables > testcase config variables
        variables = merge_variables(variables, self.__config.variables)

        # parse variables
        # 把step里的变量和config里解析过的变量再走一遍解析流程
        return self.parser.parse_variables(variables)

    def __run_step(self, step):
        """run teststep, step maybe any kind that implements IStep interface

        Args:
            step (Step): teststep

        """
        logger.info(f"run step begin: {step.name()} >>>>>>")

        # run step
        for i in range(step.retry_times + 1):
            try:
                if ALLURE is not None:
                    with ALLURE.step(f"step: {step.name()}"):
                        step_result: StepResult = step.run(self)
                else:
                    step_result: StepResult = step.run(self)
                break
            except ValidationFailure:
                if i == step.retry_times:
                    raise
                else:
                    logger.warning(
                        f"run step {step.name()} validation failed,wait {step.retry_interval} sec and try again"
                    )
                    time.sleep(step.retry_interval)
                    logger.info(
                        f"run step retry ({i + 1}/{step.retry_times} time): {step.name()} >>>>>>"
                    )

        # save extracted variables to session variables
        # 在用例1调用用例2的过程中，把用例2里导出变量更新到用例1的runner.__session_variables里
        # step_result.export_vars来自用例2里的runner.__session_variables里，注意是不同的runner实例
        self.__session_variables.update(step_result.export_vars)
        # update testcase summary
        self.__step_results.append(step_result)

        logger.info(f"run step end: {step.name()} <<<<<<\n")

    def test_start(self, param: Dict = None) -> "SessionRunner":
        """main entrance, discovered by pytest"""
        ga4_client.send_event("test_start")
        print("\n")
        self.__init()
        self.__parse_config(param)

        if ALLURE is not None and not self.__is_referenced:
            # update allure report meta
            ALLURE.dynamic.title(self.__config.name)
            ALLURE.dynamic.description(f"TestCase ID: {self.case_id}")

        logger.info(
            f"Start to run testcase: {self.__config.name}, TestCase ID: {self.case_id}"
        )

        # 设置为 "DEBUG" 意味着所有的DEBUG、INFO、WARNING、ERROR和CRITICAL级别的日志都将被记录到指定的位置
        logger.add(self.__log_path, format=LOGGER_FORMAT, level="DEBUG")
        self.__start_at = time.time()
        try:
            # run step in sequential order
            for step in self.teststeps:
                self.__run_step(step)
        finally:
            logger.info(f"generate testcase log: {self.__log_path}")
            if ALLURE is not None:
                ALLURE.attach.file(
                    self.__log_path,
                    name="all log",
                    attachment_type=ALLURE.attachment_type.TEXT,
                )

        self.__duration = time.time() - self.__start_at
        return self


class HttpRunner(SessionRunner):
    # split SessionRunner to keep consistent with golang version
    pass
