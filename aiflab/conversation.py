"""Token accounting for one agent conversation.

Model of a tool-using conversation (standard for chat-style APIs):
* Every model turn re-reads the whole context: system prompt + tool definitions +
  user question + all previous tool calls and tool results.
* Independent tool calls are issued in parallel in one turn (best case).

Metrics
* ``turns``             model inference passes, including the final answer.
* ``result_tokens``     tokens of tool results placed in the context.
* ``context_tokens``    tokens in the context at the final turn (peak context size).
* ``cumulative_input``  input tokens summed over all turns — what you pay without
                        prompt caching. With caching, repeated prefixes get cheaper,
                        but ``context_tokens`` and ``turns`` still apply.
* ``output_tokens``     tokens the model generates (tool calls, code, final answer).

Tokenizer: tiktoken ``o200k_base``, used as a stand-in. Other models' tokenizers give
different absolute counts; the comparisons in this lab are ratios between
configurations that use the same tokenizer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import tiktoken

_ENC = tiktoken.get_encoding("o200k_base")

SYSTEM_PROMPT = ("You are a claims operations assistant for an insurer. Answer the adjuster's question "
                 "using the available tools. Be accurate and concise.")


def ntok(text: str) -> int:
    return len(_ENC.encode(text))


@dataclass
class Conversation:
    tool_defs_text: str
    question: str
    turns: int = 0
    result_tokens: int = 0
    output_tokens: int = 0
    cumulative_input: int = 0
    history_tokens: int = 0
    tool_calls: int = 0
    trace: list = field(default_factory=list)

    @property
    def base_tokens(self) -> int:
        return ntok(SYSTEM_PROMPT) + ntok(self.tool_defs_text) + ntok(self.question)

    def turn(self, calls, results):
        """One model turn that emits ``calls`` (list of (name, args)); ``results`` are their tool outputs."""
        assert len(calls) == len(results)
        self.turns += 1
        self.cumulative_input += self.base_tokens + self.history_tokens
        out = sum(ntok(json.dumps({"name": n, "input": a})) for n, a in calls)
        res = sum(ntok(r) for r in results)
        self.output_tokens += out
        self.result_tokens += res
        self.history_tokens += out + res
        self.tool_calls += len(calls)
        self.trace.append({"calls": [n for n, _ in calls], "result_tokens": res})

    def code_turn(self, code: str, stdout: str):
        self.turns += 1
        self.cumulative_input += self.base_tokens + self.history_tokens
        out, res = ntok(code), ntok(stdout)
        self.output_tokens += out
        self.result_tokens += res
        self.history_tokens += out + res
        self.tool_calls += 1
        self.trace.append({"calls": ["run_python"], "result_tokens": res})

    def answer(self, text: str):
        self.turns += 1
        self.cumulative_input += self.base_tokens + self.history_tokens
        self.output_tokens += ntok(text)

    def summary(self) -> dict:
        return {"turns": self.turns, "tool_calls": self.tool_calls, "tool_def_tokens": ntok(self.tool_defs_text),
                "result_tokens": self.result_tokens, "context_tokens": self.base_tokens + self.history_tokens,
                "cumulative_input": self.cumulative_input, "output_tokens": self.output_tokens}
