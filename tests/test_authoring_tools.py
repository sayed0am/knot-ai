"""The @tool decorator: schema derivation, naming, rejection rules, execution."""

from __future__ import annotations

from pathlib import Path

import pytest
from authoring_fixtures import write_files

from knot.authoring.tools import ToolDefinitionError, compile_tool_module
from knot.core.tools import execute_tool
from knot.providers.messages import ToolCall


def test_tool_name_comes_from_file_stem(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "get_invoice.py": """
                from knot.authoring.tools import tool


                @tool
                def get_invoice(invoice_id: str) -> str:
                    \"\"\"Look up an invoice by id.\"\"\"
                    return f"invoice:{invoice_id}"
            """
        },
    )

    agent_tool = compile_tool_module(tmp_path / "get_invoice.py")

    assert agent_tool.name == "get_invoice"
    assert agent_tool.description == "Look up an invoice by id."
    assert agent_tool.idempotent is False


def test_tool_name_override_is_validated_against_id_pattern(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "whatever.py": """
                from knot.authoring.tools import tool


                @tool(name="renamed_tool", idempotent=True)
                def some_fn(x: int) -> int:
                    return x
            """
        },
    )

    agent_tool = compile_tool_module(tmp_path / "whatever.py")

    assert agent_tool.name == "renamed_tool"
    assert agent_tool.idempotent is True


def test_invalid_name_override_is_rejected(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "whatever.py": """
                from knot.authoring.tools import tool


                @tool(name="Not Valid")
                def some_fn(x: int) -> int:
                    return x
            """
        },
    )

    with pytest.raises(ToolDefinitionError, match="invalid"):
        compile_tool_module(tmp_path / "whatever.py")


def test_schema_derivation_types_defaults_and_annotated_description(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "search.py": """
                from typing import Annotated

                from knot.authoring.tools import tool


                @tool
                def search(
                    query: Annotated[str, "the search text"],
                    limit: int = 10,
                ) -> str:
                    \"\"\"Search for things.\"\"\"
                    return f"{query}:{limit}"
            """
        },
    )

    agent_tool = compile_tool_module(tmp_path / "search.py")

    props = agent_tool.parameters["properties"]
    assert props["query"]["type"] == "string"
    assert props["query"]["description"] == "the search text"
    assert props["limit"]["type"] == "integer"
    assert props["limit"]["default"] == 10
    assert agent_tool.parameters["required"] == ["query"]
    assert "title" not in agent_tool.parameters


def test_docstring_first_paragraph_becomes_description(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "thing.py": '''
                from knot.authoring.tools import tool


                @tool
                def thing(x: int) -> int:
                    """First paragraph only.

                    This second paragraph should not appear in the description.
                    """
                    return x
            '''
        },
    )

    agent_tool = compile_tool_module(tmp_path / "thing.py")

    assert agent_tool.description == "First paragraph only."


def test_rejects_var_positional_and_var_keyword(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "bad.py": """
                from knot.authoring.tools import tool


                @tool
                def bad(*args, **kwargs) -> None:
                    pass
            """
        },
    )

    with pytest.raises(ToolDefinitionError, match=r"\*args|\*\*kwargs"):
        compile_tool_module(tmp_path / "bad.py")


def test_rejects_missing_type_annotation(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "bad.py": """
                from knot.authoring.tools import tool


                @tool
                def bad(x) -> None:
                    pass
            """
        },
    )

    with pytest.raises(ToolDefinitionError, match="annotation"):
        compile_tool_module(tmp_path / "bad.py")


def test_rejects_zero_decorated_functions(tmp_path: Path) -> None:
    write_files(tmp_path, {"empty.py": "def plain(x: int) -> int:\n    return x\n"})

    with pytest.raises(ToolDefinitionError, match="no @tool"):
        compile_tool_module(tmp_path / "empty.py")


def test_rejects_more_than_one_decorated_function(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "double.py": """
                from knot.authoring.tools import tool


                @tool
                def first(x: int) -> int:
                    return x


                @tool
                def second(x: int) -> int:
                    return x
            """
        },
    )

    with pytest.raises(ToolDefinitionError, match="more than one"):
        compile_tool_module(tmp_path / "double.py")


async def test_argument_validation_failure_is_an_error_result_not_an_exception(
    tmp_path: Path,
) -> None:
    write_files(
        tmp_path,
        {
            "get_invoice.py": """
                from knot.authoring.tools import tool


                @tool
                def get_invoice(invoice_id: str) -> str:
                    \"\"\"Look up an invoice.\"\"\"
                    return f"invoice:{invoice_id}"
            """
        },
    )
    agent_tool = compile_tool_module(tmp_path / "get_invoice.py")
    call = ToolCall(id="c1", name="get_invoice", arguments={"invoice_id": 123, "extra": "nope"})

    result, is_error = await execute_tool(agent_tool, call)

    assert is_error is True
    assert result.text  # some validation detail was captured, not a bare exception


async def test_sync_function_executes_via_thread(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "add.py": """
                from knot.authoring.tools import tool


                @tool
                def add(a: int, b: int) -> int:
                    \"\"\"Add two numbers.\"\"\"
                    return a + b
            """
        },
    )
    agent_tool = compile_tool_module(tmp_path / "add.py")
    call = ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})

    result, is_error = await execute_tool(agent_tool, call)

    assert is_error is False
    assert result.text == "5"


async def test_async_function_executes_directly(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "greet.py": """
                from knot.authoring.tools import tool


                @tool
                async def greet(name: str) -> str:
                    \"\"\"Greet someone.\"\"\"
                    return f"hello {name}"
            """
        },
    )
    agent_tool = compile_tool_module(tmp_path / "greet.py")
    call = ToolCall(id="c1", name="greet", arguments={"name": "ada"})

    result, is_error = await execute_tool(agent_tool, call)

    assert is_error is False
    assert result.text == "hello ada"


def test_field_description_default_is_supported(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "note.py": """
                from pydantic import Field

                from knot.authoring.tools import tool


                @tool
                def note(text: str = Field(default="", description="freeform note")) -> str:
                    \"\"\"Store a note.\"\"\"
                    return text
            """
        },
    )

    agent_tool = compile_tool_module(tmp_path / "note.py")

    props = agent_tool.parameters["properties"]
    assert props["text"]["description"] == "freeform note"
    assert props["text"]["default"] == ""
