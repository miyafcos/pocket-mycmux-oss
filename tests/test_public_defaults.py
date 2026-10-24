import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_optional_mail_and_research_are_disabled_by_default():
    module = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    defaults = next(node for node in module.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_machine_defaults")
    features = []
    origins = []
    for node in ast.walk(defaults):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "features":
                features.append(ast.literal_eval(value))
            if isinstance(key, ast.Constant) and key.value == "publicOrigin":
                origins.append(ast.literal_eval(value))
    assert len(features) == 2
    assert all(value == {"mail": False, "research": False} for value in features)
    assert origins == ["", ""]


def test_hub_has_no_author_server_default():
    module = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    values = [node.value.value for node in module.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "DEFAULT_HUB_ORIGIN" for target in node.targets)
              and isinstance(node.value, ast.Constant)]
    assert values == [""]


def load_function(name, namespace, class_name=None):
    module = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    container = module if class_name is None else next(
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    function = next(node for node in container.body if isinstance(node, ast.FunctionDef) and node.name == name)
    isolated = ast.Module(body=[function], type_ignores=[])
    exec(compile(isolated, "server-extracted-function", "exec"), namespace)
    return namespace[name]


def test_empty_origin_is_rejected_even_when_present_in_allowlist():
    from types import SimpleNamespace
    check = load_function("_origin_allowed", {}, "PocketHandler")
    handler = SimpleNamespace(server=SimpleNamespace(allowed_origins={"", "https://example.test"}))
    assert check(handler, "") is False
    assert check(handler, None) is False
    assert check(handler, "null") is False
    assert check(handler, "https://example.test") is True


def test_unconfigured_hub_returns_no_url_without_filesystem_access():
    check = load_function("hub_public_url", {"Path": Path, "HUB_PUBLIC_ORIGIN": ""})
    class UnexpectedPath:
        def resolve(self):
            raise AssertionError("unconfigured hub must return before resolving")
    assert check(UnexpectedPath()) is None


def test_empty_origin_is_removed_from_constructor_allowlist():
    module = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    server = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "PocketHTTPServer")
    constructor = next(node for node in server.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assignment = next(node for node in ast.walk(constructor) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Attribute) and target.attr == "allowed_origins" for target in node.targets))
    expression = ast.Expression(body=assignment.value)
    assert eval(compile(expression, "allowlist-expression", "eval"),
                {"configured_origins": {"", "https://example.test"}}) == {"https://example.test"}
