from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from functools import wraps
from types import new_class
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import click
from humps import camelize, decamelize  # type: ignore[attr-defined]
from pydantic import BaseModel
from pydantic.json import pydantic_encoder
from pydantic.schema import model_schema
from quart import current_app, Quart, render_template_string, Response, ResponseReturnValue
from quart.cli import pass_script_info, ScriptInfo
from quart.json.provider import DefaultJSONProvider
from werkzeug.routing.converters import NumberConverter

from .mixins import create_test_client_mixin, RequestMixin, WebsocketMixin
from .typing import SecuritySchemeObject, ServerObject, TagObject
from .validation import (
    DataSource,
    QUART_SCHEMA_HEADERS_ATTRIBUTE,
    QUART_SCHEMA_QUERYSTRING_ATTRIBUTE,
    QUART_SCHEMA_REQUEST_ATTRIBUTE,
    QUART_SCHEMA_RESPONSE_ATTRIBUTE,
)

QUART_SCHEMA_HIDDEN_ATTRIBUTE = "_quart_schema_hidden"
QUART_SCHEMA_TAG_ATTRIBUTE = "_quart_schema_tag"
QUART_SCHEMA_SECURITY_ATTRIBUTE = "_quart_schema_security_tag"
QUART_SCHEMA_DEPRECATED = "_quart_schema_deprecated"
REF_PREFIX = "#/components/schemas/"

PATH_RE = re.compile("<(?:[^:]*:)?([^>]+)>")

REDOC_TEMPLATE = """
<head>
  <title>{{ title }}</title>
  <style>
    body {
      margin: 0;
      padding: 0;
    }
  </style>
</head>
<body>
  <redoc spec-url="{{ openapi_path }}"></redoc>
  <script src="{{ redoc_js_url }}"></script>
  <noscript>This page requires Javascript to function.</noscript>
</body>
"""

SWAGGER_TEMPLATE = """
<head>
  <link type="text/css" rel="stylesheet" href="{{ swagger_css_url }}">
  <title>{{ title }}</title>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{{ swagger_js_url }}"></script>
  <script>
    const ui = SwaggerUIBundle({
      deepLinking: true,
      dom_id: "#swagger-ui",
      layout: "BaseLayout",
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset
      ],
      showExtensions: true,
      showCommonExtensions: true,
      url: "{{ openapi_path }}"
    });
  </script>
</body>
"""


def hide_route(func: Callable) -> Callable:
    """Mark the func as hidden.

    This will prevent the route from being included in the
    autogenerated documentation.
    """
    setattr(func, QUART_SCHEMA_HIDDEN_ATTRIBUTE, True)
    return func


class PydanticJSONEncoder(json.JSONEncoder):
    def default(self, object_: Any) -> Any:
        return pydantic_encoder(object_)


class CasingJSONEncoder(PydanticJSONEncoder):
    def encode(self, object_: Any) -> Any:
        if isinstance(object_, (list, Mapping)):
            object_ = camelize(object_)
        return super().encode(camelize(object_))


class CasingJSONDecoder(json.JSONDecoder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, object_hook=self.object_hook, **kwargs)

    def object_hook(self, object_: dict) -> Any:
        return decamelize(object_)


class JSONProvider(DefaultJSONProvider):
    def __init__(self, app: Quart, convert_casing: bool) -> None:
        super().__init__(app)
        self._convert_casing = convert_casing

    def dumps(self, object_: Any, **kwargs: Any) -> str:
        if self._convert_casing:
            kwargs["cls"] = CasingJSONEncoder
        else:
            kwargs["cls"] = PydanticJSONEncoder
        return super().dumps(object_, **kwargs)

    def loads(self, object_: str | bytes, **kwargs: Any) -> Any:
        kwargs["cls"] = CasingJSONDecoder
        return super().loads(object_, **kwargs)


class QuartSchema:

    """A Quart-Schema instance.

    This can be used to initialise Quart-Schema documentation a given
    app, either directly,

    .. code-block:: python

        app = Quart(__name__)
        QuartSchema(app)

    or via the factory pattern,

    .. code-block:: python

        quart_schema = QuartSchema()

        def create_app():
            app = Quart(__name__)
            quart_schema.init_app(app)
            return app

    This can be customised using the following arguments,

    Arguments:
        openapi_path: The path used to serve the openapi json on, or None
            to disable documentation.
        redoc_ui_path: The path used to serve the documentation UI using
            redoc or None to disable redoc documentation.
        swagger_ui_path: The path used to serve the documentation UI using
            swagger or None to disable swagger documentation.
        title: The publishable title for the app.
        version: The publishable version for the app.
        security_schemes: The security schemes to be configured for this app.
        security: The security schemes to apply globally (to all routes).

    """

    def __init__(
        self,
        app: Optional[Quart] = None,
        *,
        openapi_path: Optional[str] = "/openapi.json",
        redoc_ui_path: Optional[str] = "/redocs",
        swagger_ui_path: Optional[str] = "/docs",
        title: Optional[str] = None,
        version: str = "0.1.0",
        tags: Optional[List[TagObject]] = None,
        convert_casing: bool = False,
        servers: Optional[List[ServerObject]] = None,
        security_schemes: Optional[Dict[str, SecuritySchemeObject]] = None,
        security: Optional[List[Dict[str, List[str]]]] = None,
    ) -> None:
        self.openapi_path = openapi_path
        self.redoc_ui_path = redoc_ui_path
        self.swagger_ui_path = swagger_ui_path
        self.title = title
        self.version = version
        self.tags: List[TagObject] = tags
        self.convert_casing = convert_casing
        self.servers = servers
        self.security_schemes = security_schemes
        self.security = security
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Quart) -> None:
        app.extensions["QUART_SCHEMA"] = self
        self.title = app.name if self.title is None else self.title
        app.test_client_class = new_class(
            "TestClient", (create_test_client_mixin(self.convert_casing), app.test_client_class)
        )
        app.websocket_class = new_class(  # type: ignore
            "Websocket", (WebsocketMixin, app.websocket_class)
        )
        app.json = JSONProvider(app, self.convert_casing)
        app.make_response = convert_model_result(app.make_response)  # type: ignore
        if self.convert_casing:
            app.request_class = new_class(  # type: ignore
                "Request", (RequestMixin, app.request_class)
            )

        app.config.setdefault(
            "QUART_SCHEMA_SWAGGER_JS_URL",
            "https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/4.12.0/swagger-ui-bundle.js",
        )
        app.config.setdefault(
            "QUART_SCHEMA_SWAGGER_CSS_URL",
            "https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/4.12.0/swagger-ui.min.css",
        )
        app.config.setdefault(
            "QUART_SCHEMA_REDOC_JS_URL",
            "https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js",
        )
        if self.openapi_path is not None:
            hide_route(app.send_static_file.__func__)  # type: ignore
            app.add_url_rule(self.openapi_path, "openapi", self.openapi)
            if self.redoc_ui_path is not None:
                app.add_url_rule(self.redoc_ui_path, "redoc_ui", self.redoc_ui)
            if self.swagger_ui_path is not None:
                app.add_url_rule(self.swagger_ui_path, "swagger_ui", self.swagger_ui)

        app.cli.add_command(_schema_command)

    @hide_route
    async def openapi(self) -> Response:
        openapi_schema = _build_openapi_schema(current_app, self)
        return DefaultJSONProvider(current_app._get_current_object()).response(openapi_schema)  # type: ignore # noqa: E501

    @hide_route
    async def swagger_ui(self) -> str:
        return await render_template_string(
            SWAGGER_TEMPLATE,
            title=self.title,
            openapi_path=self.openapi_path,
            swagger_js_url=current_app.config["QUART_SCHEMA_SWAGGER_JS_URL"],
            swagger_css_url=current_app.config["QUART_SCHEMA_SWAGGER_CSS_URL"],
        )

    @hide_route
    async def redoc_ui(self) -> str:
        return await render_template_string(
            REDOC_TEMPLATE,
            title=self.title,
            openapi_path=self.openapi_path,
            redoc_js_url=current_app.config["QUART_SCHEMA_REDOC_JS_URL"],
        )


@click.command("schema")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output the spec to a file given by a path.",
)
@pass_script_info
def _schema_command(info: ScriptInfo, output: Optional[str]) -> None:
    app = info.load_app()
    schema = _build_openapi_schema(app, app.extensions["QUART_SCHEMA"])
    formatted_spec = json.dumps(schema, indent=2)
    if output is not None:
        with open(output, "w") as file_:
            click.echo(formatted_spec, file=file_)
    else:
        click.echo(formatted_spec)


def _split_definitions(schema: dict) -> Tuple[dict, dict]:
    new_schema = schema.copy()
    definitions = new_schema.pop("definitions", {})
    return definitions, new_schema


def _split_convert_definitions(schema: dict, convert_casing: bool) -> Tuple[dict, dict]:
    definitions, new_schema = _split_definitions(schema)
    if convert_casing:
        new_schema = camelize(new_schema)
        definitions = {key: camelize(definition) for key, definition in definitions.items()}
    return definitions, new_schema


def convert_model_result(func: Callable) -> Callable:
    @wraps(func)
    async def decorator(result: ResponseReturnValue) -> Response:
        status_or_headers = None
        headers = None
        if isinstance(result, tuple):
            value, status_or_headers, headers = result + (None,) * (3 - len(result))
        else:
            value = result

        if is_dataclass(value):
            dict_or_value = asdict(value)
        elif isinstance(value, BaseModel):
            dict_or_value = value.dict()
        else:
            dict_or_value = value
        return await func((dict_or_value, status_or_headers, headers))

    return decorator


def tag(tags: Iterable[str]) -> Callable:
    """Add tag names to the route.

    This allows for tags to be associated with the route, thereby
    allowing control over which routes are shown in the documentation.

    Arguments:
        tags: A List (or iterable) of tags to associate.

    """

    def decorator(func: Callable) -> Callable:
        setattr(func, QUART_SCHEMA_TAG_ATTRIBUTE, set(tags))

        return func

    return decorator


def deprecated() -> Callable:
    """Mark endpoint as deprecated."""

    def decorator(func: Callable) -> Callable:
        setattr(func, QUART_SCHEMA_DEPRECATED, True)

        return func

    return decorator


def security_scheme(schemes: Iterable[Dict[str, List[str]]]) -> Callable:
    """Add security schemes to the route.

    Allows security schemes to be associated with this route. Security
    schemes first need to be defined on the constructor and can be
    referenced here by name.

    Arguments:
        schemes: A List (or iterable) of security schemes to associate.

    """

    def decorator(func: Callable) -> Callable:
        setattr(func, QUART_SCHEMA_SECURITY_ATTRIBUTE, schemes)

        return func

    return decorator


def _build_openapi_schema(app: Quart, extension: QuartSchema) -> dict:
    paths: Dict[str, dict] = {}
    components = {"schemas": {}}  # type: ignore
    for rule in app.url_map.iter_rules():
        if rule.websocket:
            continue

        func = app.view_functions[rule.endpoint]
        if getattr(func, QUART_SCHEMA_HIDDEN_ATTRIBUTE, False):
            continue

        operation_object = {  # type: ignore
            "parameters": [],
            "responses": {},
        }
        if func.__doc__ is not None:
            summary, *description = inspect.getdoc(func).splitlines()
            operation_object["description"] = "\n".join(description)
            operation_object["summary"] = summary

        if getattr(func, QUART_SCHEMA_TAG_ATTRIBUTE, None) is not None:
            operation_object["tags"] = list(getattr(func, QUART_SCHEMA_TAG_ATTRIBUTE))

        if getattr(func, QUART_SCHEMA_DEPRECATED, None):
            operation_object["deprecated"] = True

        if getattr(func, QUART_SCHEMA_SECURITY_ATTRIBUTE, None) is not None:
            operation_object["security"] = list(getattr(func, QUART_SCHEMA_SECURITY_ATTRIBUTE))

        response_models = getattr(func, QUART_SCHEMA_RESPONSE_ATTRIBUTE, {})
        for status_code in response_models.keys():
            model_class, headers_model_class = response_models[status_code]
            schema = model_schema(model_class, ref_prefix=REF_PREFIX)
            definitions, schema = _split_convert_definitions(schema, extension.convert_casing)
            components["schemas"].update(definitions)
            response_object = {
                "content": {
                    "application/json": {
                        "schema": schema,
                    },
                },
                "description": "",
            }
            if model_class.__doc__ is not None:
                response_object["description"] = inspect.getdoc(model_class)

            if headers_model_class is not None:
                schema = model_schema(headers_model_class, ref_prefix=REF_PREFIX)
                definitions, schema = _split_definitions(schema)
                components["schemas"].update(definitions)
                response_object["content"]["headers"] = {  # type: ignore
                    name.replace("_", "-"): {
                        "schema": type_,
                    }
                    for name, type_ in schema["properties"].items()
                }
            operation_object["responses"][status_code] = response_object  # type: ignore

        request_data = getattr(func, QUART_SCHEMA_REQUEST_ATTRIBUTE, None)
        if request_data is not None:
            schema = model_schema(request_data[0], ref_prefix=REF_PREFIX)
            definitions, schema = _split_convert_definitions(schema, extension.convert_casing)
            components["schemas"].update(definitions)

            if request_data[1] == DataSource.JSON:
                encoding = "application/json"
            else:
                encoding = "application/x-www-form-urlencoded"

            operation_object["requestBody"] = {
                "content": {
                    encoding: {
                        "schema": schema,
                    },
                },
            }

        querystring_model = getattr(func, QUART_SCHEMA_QUERYSTRING_ATTRIBUTE, None)
        if querystring_model is not None:
            schema = model_schema(querystring_model, ref_prefix=REF_PREFIX)
            definitions, schema = _split_convert_definitions(schema, extension.convert_casing)
            components["schemas"].update(definitions)
            for name, type_ in schema["properties"].items():
                param = {"name": name, "in": "query", "schema": type_}

                for attribute in ("description", "required", "deprecated"):
                    if attribute in type_:
                        param[attribute] = type_.pop(attribute)

                operation_object["parameters"].append(param)  # type: ignore

        headers_model = getattr(func, QUART_SCHEMA_HEADERS_ATTRIBUTE, None)
        if headers_model is not None:
            schema = model_schema(headers_model, ref_prefix=REF_PREFIX)
            definitions, schema = _split_definitions(schema)
            components["schemas"].update(definitions)
            for name, type_ in schema["properties"].items():
                param = {"name": name.replace("_", "-"), "in": "header", "schema": type_}

                for attribute in ("description", "required", "deprecated"):
                    if attribute in type_:
                        param[attribute] = type_.pop(attribute)

                operation_object["parameters"].append(param)  # type: ignore

        for name, converter in rule._converters.items():
            type_ = "string"
            if isinstance(converter, NumberConverter):
                type_ = "number"

            operation_object["parameters"].append(  # type: ignore
                {
                    "name": name,
                    "in": "path",
                    "required": True,
                    "schema": {"type": type_},
                }
            )

        path = re.sub(PATH_RE, r"{\1}", rule.rule)
        paths.setdefault(path, {})

        for method in rule.methods:
            if method == "HEAD" or (method == "OPTIONS" and rule.provide_automatic_options):  # type: ignore  # noqa: E501
                continue
            paths[path][method.lower()] = operation_object

    if extension.security_schemes is not None:
        components["securitySchemes"] = extension.security_schemes

    openapi_schema: dict = {
        "openapi": "3.0.3",
        "info": {
            "title": extension.title,
            "version": extension.version,
        },
        "components": components,
        "paths": paths,
    }
    if extension.tags is not None:
        openapi_schema["tags"] = extension.tags
    if extension.security is not None:
        openapi_schema["security"] = extension.security
    if extension.servers is not None:
        openapi_schema["servers"] = extension.servers
    return openapi_schema
