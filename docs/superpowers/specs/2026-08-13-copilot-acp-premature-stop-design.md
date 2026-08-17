# Copilot ACP Premature Stop Recovery Design

## Problem

When Hermes uses the `copilot-acp` provider, Copilot can respond to an
action-oriented request with a short progress acknowledgement instead of the
structured `<tool_call>` block requested by the adapter. The adapter converts
any response without a parsed tool call to `finish_reason="stop"`, and the
conversation loop therefore accepts the acknowledgement as a completed turn.
The observed result is `tool_turns=0` and a reply such as an announcement that
work will begin, with no work actually performed.

## Scope

Add a narrow, provider-specific recovery for likely action acknowledgements
returned by Copilot ACP. Do not redesign ACP transport, add native MCP support,
or parse arbitrary shell/XML prose as executable commands.

## Design

The Copilot ACP adapter will classify a response as a recoverable action
acknowledgement only when all of these conditions hold:

- the request exposed at least one Hermes tool;
- the response contains no parsed structured tool call;
- the response is short and matches the existing action-acknowledgement
  semantics used elsewhere in Hermes;
- the response does not look like a substantive final answer.

The adapter will surface this classification without manufacturing or
executing a tool call. The conversation loop will append the acknowledgement
as an interim assistant message followed by a synthetic user nudge requiring
the next response to emit a real structured `<tool_call>` or provide a genuine
final answer. Recovery is limited to two consecutive attempts. A real tool
call or substantive answer resets the counter. At the retry limit Hermes
returns the original response rather than looping indefinitely.

The recovery transcript must preserve strict user/assistant role alternation
and must not mutate earlier conversation context. Synthetic recovery messages
must not become durable session history.

## Safety

Hermes will never execute text that merely resembles a command. Only existing
structured tool calls continue to reach the tool executor. The classifier is
conservative and applies only to `copilot-acp`, only when tools were supplied,
and only to short action acknowledgements. Ordinary conversational responses
remain normal `stop` completions.

## Testing

Regression tests will prove the red/green behavior for:

1. a short action acknowledgement with available tools triggers recovery;
2. a subsequent structured tool call is returned normally;
3. a normal short factual answer remains a completed text response;
4. a request without tools remains a completed text response;
5. two failed recovery attempts stop safely without an infinite loop;
6. recovery-only transcript scaffolding is not persisted.

Focused Copilot ACP and conversation-loop tests will run first, followed by the
broader related agent test suites required by the repository.

## Success Criteria

- The reproduced `tool_turns=0` premature-stop path receives a bounded chance
  to emit a real tool call.
- Genuine final answers are not forced into tool use.
- No prose command is executed.
- Existing Copilot ACP response parsing and normal conversation completion
  behavior remain green.
