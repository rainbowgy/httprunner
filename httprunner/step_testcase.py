from typing import Callable, Text

from loguru import logger

from httprunner import exceptions
from httprunner.models import IStep, StepResult, TStep, TestCaseSummary
from httprunner.runner import HttpRunner
from httprunner.step_request import call_hooks


def run_step_testcase(runner: HttpRunner, step: TStep) -> StepResult:
    """run teststep: referenced testcase"""
    step_result = StepResult(name=step.name, step_type="testcase")
    step_variables = runner.merge_step_variables(step.variables)
    step_export = step.export

    # setup hooks
    if step.setup_hooks:
        # call_hooks实际上是走parse_data逻辑来调用fun
        call_hooks(runner, step.setup_hooks, step_variables, "setup testcase")

    # TODO: override testcase with current step name/variables/export

    # step.testcase is a referenced testcase, e.g. RequestWithFunctions
    # step.testcase()相当于在调用用例类RequestWithFunctions(),实例化了一下，继承httprunner，注意是个新的runner实例
    # 传递当前用例的参数给到RequestWithFunctions()，再调用test_start（），执行RequestWithFunctions()用例
    # 虽然是不同的runner实例，但是with_session(runner.session)共享了同一个httpsession实例
    ref_case_runner = step.testcase()
    ref_case_runner.set_referenced().with_session(runner.session).with_case_id(
        runner.case_id
    ).with_variables(step_variables).with_export(step_export).test_start()
    # with_variables把当前步骤的变量和config的变量，更新到runner的__session_variables里
    # 这样才能把当前测试变量传递给另一个test的test_start()里
    # with_export把步骤里声明导出的变量更新到runner的__export里

    # teardown hooks
    if step.teardown_hooks:
        call_hooks(runner, step.teardown_hooks, step.variables, "teardown testcase")

    summary: TestCaseSummary = ref_case_runner.get_summary()
    step_result.data = summary.step_results  # list of step data
    # get_summary方法里会调用get_export_variables方法，把config或者step定义的export变量
    # 对应runner里的__export和__config.export，从__session_variables里取值导出
    step_result.export_vars = summary.in_out.export_vars
    step_result.success = summary.success

    if step_result.export_vars:
        logger.info(f"export variables: {step_result.export_vars}")

    return step_result


class StepRefCase(IStep):
    def __init__(self, step: TStep):
        self.__step = step

    def teardown_hook(self, hook: Text, assign_var_name: Text = None) -> "StepRefCase":
        if assign_var_name:
            self.__step.teardown_hooks.append({assign_var_name: hook})
        else:
            self.__step.teardown_hooks.append(hook)

        return self

    def export(self, *var_name: Text) -> "StepRefCase":
        self.__step.export.extend(var_name)
        return self

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return f"request-{self.__step.request.method}"

    def run(self, runner: HttpRunner):
        return run_step_testcase(runner, self.__step)


class RunTestCase(object):
    def __init__(self, name: Text):
        self.__step = TStep(name=name)

    def with_variables(self, **variables) -> "RunTestCase":
        self.__step.variables.update(variables)
        return self

    def with_retry(self, retry_times, retry_interval) -> "RunTestCase":
        self.__step.retry_times = retry_times
        self.__step.retry_interval = retry_interval
        return self

    def setup_hook(self, hook: Text, assign_var_name: Text = None) -> "RunTestCase":
        if assign_var_name:
            self.__step.setup_hooks.append({assign_var_name: hook})
        else:
            self.__step.setup_hooks.append(hook)

        return self

    def call(self, testcase: Callable) -> StepRefCase:
        if issubclass(testcase, HttpRunner):
            # referenced testcase object
            self.__step.testcase = testcase
        else:
            raise exceptions.ParamsError(
                f"Invalid teststep referenced testcase: {testcase}"
            )

        return StepRefCase(self.__step)
