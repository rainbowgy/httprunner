import json
import time

import requests
import urllib3
from loguru import logger
from requests import Request, Response
from requests.exceptions import (
    InvalidSchema,
    InvalidURL,
    MissingSchema,
    RequestException,
)

from httprunner.models import RequestData, ResponseData
from httprunner.models import SessionData, ReqRespData
from httprunner.utils import lower_dict_keys, omit_long_data

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ApiResponse(Response):
    def raise_for_status(self):
        if hasattr(self, "error") and self.error:
            raise self.error
        Response.raise_for_status(self)


def get_req_resp_record(resp_obj: Response) -> ReqRespData:
    """get request and response info from Response() object."""

    def log_print(req_or_resp, r_type):
        msg = f"\n================== {r_type} details ==================\n"
        for key, value in req_or_resp.dict().items():
            if isinstance(value, dict) or isinstance(value, list):
                value = json.dumps(value, indent=4, ensure_ascii=False)

            msg += "{:<8} : {}\n".format(key, value)
        logger.debug(msg)

    # record actual request info
    request_headers = dict(resp_obj.request.headers)
    request_cookies = resp_obj.request._cookies.get_dict()

    request_body = resp_obj.request.body
    if request_body is not None:
        try:
            request_body = json.loads(request_body)
        except json.JSONDecodeError:
            # str: a=1&b=2
            pass
        except UnicodeDecodeError:
            # bytes/bytearray: request body in protobuf
            pass
        except TypeError:
            # neither str nor bytes/bytearray, e.g. <MultipartEncoder>
            pass

        request_content_type = lower_dict_keys(request_headers).get("content-type")
        if request_content_type and "multipart/form-data" in request_content_type:
            # upload file type
            request_body = "upload file stream (OMITTED)"

    request_data = RequestData(
        method=resp_obj.request.method,
        url=resp_obj.request.url,
        headers=request_headers,
        cookies=request_cookies,
        body=request_body,
    )

    # log request details in debug mode
    log_print(request_data, "request")

    # record response info
    resp_headers = dict(resp_obj.headers)
    lower_resp_headers = lower_dict_keys(resp_headers)
    content_type = lower_resp_headers.get("content-type", "")

    if "image" in content_type:
        # response is image type, record bytes content only
        response_body = resp_obj.content
    else:
        try:
            # try to record json data
            response_body = resp_obj.json()
        except ValueError:
            # only record at most 512 text charactors
            resp_text = resp_obj.text
            response_body = omit_long_data(resp_text)

    response_data = ResponseData(
        status_code=resp_obj.status_code,
        cookies=resp_obj.cookies or {},
        encoding=resp_obj.encoding,
        headers=resp_headers,
        content_type=content_type,
        body=response_body,
    )

    # log response details in debug mode
    log_print(response_data, "response")

    req_resp_data = ReqRespData(request=request_data, response=response_data)
    return req_resp_data


class HttpSession(requests.Session):
    """
    Class for performing HTTP requests and holding (session-) cookies between requests (in order
    to be able to log in and out of websites). Each request is logged so that HttpRunner can
    display statistics.

    This is a slightly extended version of `python-request <http://python-requests.org>`_'s
    :py:class:`requests.Session` class and mostly this class works exactly the same.
    """

    def __init__(self):
        # 使用 requests.Session 对象可以让你进行更高效的 HTTP 请求，因为它可以重用底层的 TCP 连接，
        # 并且可以在多个请求之间保持某些参数（如 headers、cookies、auth 等）不变。
        # 当你需要发送多个请求到同一个服务器时，使用 Session 对象是特别有用的，因为它可以减少 TCP 连接的建立和关闭的开销，从而提高性能
        super(HttpSession, self).__init__()
        # 初始化请求模型request session data, including request, response, validators and stat data
        self.data = SessionData()

    def update_last_req_resp_record(self, resp_obj):
        """
        update request and response info from Response() object.
        """
        # TODO: fix
        self.data.req_resps.pop()
        self.data.req_resps.append(get_req_resp_record(resp_obj))

    def request(self, method, url, name=None, **kwargs):
        """
        Constructs and sends a :py:class:`requests.Request`.
        Returns :py:class:`requests.Response` object.

        :param method:
            method for the new :class:`Request` object.
        :param url:
            URL for the new :class:`Request` object.
        :param name: (optional)
            Placeholder, make compatible with Locust's HttpSession
        :param params: (optional)
            Dictionary or bytes to be sent in the query string for the :class:`Request`.
        :param data: (optional)
            Dictionary or bytes to send in the body of the :class:`Request`.
        :param headers: (optional)
            Dictionary of HTTP Headers to send with the :class:`Request`.
        :param cookies: (optional)
            Dict or CookieJar object to send with the :class:`Request`.
        :param files: (optional)
            Dictionary of ``'filename': file-like-objects`` for multipart encoding upload.
        :param auth: (optional)
            Auth tuple or callable to enable Basic/Digest/Custom HTTP Auth.
        :param timeout: (optional)
            How long to wait for the server to send data before giving up, as a float, or \
            a (`connect timeout, read timeout <user/advanced.html#timeouts>`_) tuple.
            :type timeout: float or tuple
        :param allow_redirects: (optional)
            Set to True by default.
        :type allow_redirects: bool
        :param proxies: (optional)
            Dictionary mapping protocol to the URL of the proxy.
        :param stream: (optional)
            whether to immediately download the response content. Defaults to ``False``.
        :param verify: (optional)
            if ``True``, the SSL cert will be verified. A CA_BUNDLE path can also be provided.
        :param cert: (optional)
            if String, path to ssl client cert file (.pem). If Tuple, ('cert', 'key') pair.
        """
        # 这里data后面添加完请求信息干啥用了呢？赋值给step_request里的session_data了
        self.data = SessionData()

        # timeout default to 120 seconds
        # setdefault，如果该键已经存在，则不会改变其值；如果该键不存在，则设置其值
        kwargs.setdefault("timeout", 120)

        # set stream to True, in order to get client/server IP/Port
        kwargs["stream"] = True

        start_timestamp = time.time()
        response = self._send_request_safe_mode(method, url, **kwargs)
        # 以ms为单位，四舍五入保留两位小数
        response_time_ms = round((time.time() - start_timestamp) * 1000, 2)

        try:
            client_ip, client_port = response.raw._connection.sock.getsockname()
            self.data.address.client_ip = client_ip
            self.data.address.client_port = client_port
            logger.debug(f"client IP: {client_ip}, Port: {client_port}")
        except Exception:
            pass

        try:
            server_ip, server_port = response.raw._connection.sock.getpeername()
            self.data.address.server_ip = server_ip
            self.data.address.server_port = server_port
            logger.debug(f"server IP: {server_ip}, Port: {server_port}")
        except Exception:
            pass

        # get length of the response content
        content_size = int(dict(response.headers).get("content-length") or 0)

        # record the consumed time
        self.data.stat.response_time_ms = response_time_ms
        # http请求花费的时间，ms
        self.data.stat.elapsed_ms = response.elapsed.microseconds / 1000.0
        self.data.stat.content_size = content_size

        # record request and response histories, include 30X redirection
        # 创建一个包含所有重定向响应（包括最终响应）的列表
        response_list = response.history + [response]
        # 记录每个请求的请求模型和响应模型内容
        self.data.req_resps = [
            get_req_resp_record(resp_obj) for resp_obj in response_list
        ]

        try:
            # 检查HTTP响应的状态码，如果响应状态码表示发生了错误（即不是200-299范围内的状态码），则抛出一个HTTPError异常
            response.raise_for_status()
        # 异常时执行代码
        except RequestException as ex:
            logger.error(f"{str(ex)}")
        # 没有异常时执行代码
        else:
            logger.info(
                f"status_code: {response.status_code}, "
                f"response_time(ms): {response_time_ms} ms, "
                f"response_length: {content_size} bytes"
            )

        return response

    def _send_request_safe_mode(self, method, url, **kwargs):
        """
        Send a HTTP request, and catch any exception that might occur due to connection problems.
        Safe mode has been removed from requests 1.x.
        """
        try:
            return requests.Session.request(self, method, url, **kwargs)
        # 如果 URL 缺少模式（如 http:// 或 https://）、模式无效或 URL 本身无效
        except (MissingSchema, InvalidSchema, InvalidURL):
            raise
        except RequestException as ex:
            # 创建一个 ApiResponse 对象来封装错误信息。这个对象包含异常、一个状态码
            resp = ApiResponse()
            resp.error = ex
            resp.status_code = 0  # with this status_code, content returns None
            # Request(method, url).prepare() 创建了一个准备好的请求对象，但它并没有实际发送。
            # 它只是根据给定的方法和 URL 创建了一个请求对象
            resp.request = Request(method, url).prepare()
            return resp
