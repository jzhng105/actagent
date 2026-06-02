import type {
	AgentModel,
	AgentModelEvent,
	AgentModelRequest,
	AgentTool,
} from "@cline/shared";
import { describe, expect, it } from "vitest";
import { AgentRuntime } from "./index";

/**
 * Minimal scripted model: each step receives the request the runtime built and
 * returns the events to stream back. Requests are captured for assertions.
 */
class ScriptedModel implements AgentModel {
	public readonly requests: AgentModelRequest[] = [];

	constructor(
		private readonly steps: Array<
			(request: AgentModelRequest) => Iterable<AgentModelEvent>
		>,
	) {}

	async stream(
		request: AgentModelRequest,
	): Promise<AsyncIterable<AgentModelEvent>> {
		this.requests.push(request);
		const step = this.steps.shift();
		if (!step) {
			throw new Error("No scripted model step available");
		}
		const events = step(request);
		return (async function* () {
			for (const event of events) {
				yield event;
			}
		})();
	}
}

const normalTool: AgentTool<{ value: string }, { ok: true }> = {
	name: "echo",
	description: "Echo input text",
	inputSchema: { type: "object" },
	async execute() {
		return { ok: true };
	},
};

const deferredTool: AgentTool<Record<string, never>, { shot: string }> = {
	name: "capture_screen",
	description: "Take a screenshot of the current display",
	searchHint: "screenshot image png",
	defer: true,
	inputSchema: { type: "object" },
	async execute() {
		return { shot: "data:image/png;base64,xxx" };
	},
};

function toolNames(request: AgentModelRequest): string[] {
	return request.tools.map((tool) => tool.name).sort();
}

describe("AgentRuntime deferred tools", () => {
	it("hides deferred tools and advertises the search tool initially", async () => {
		const model = new ScriptedModel([
			() => [
				{ type: "text-delta", text: "done" },
				{ type: "finish", reason: "stop" },
			],
		]);
		const runtime = new AgentRuntime({
			model,
			tools: [normalTool, deferredTool],
		});

		await runtime.run("hi");

		expect(toolNames(model.requests[0])).toEqual(["echo", "search_tools"]);
		// The deferred tool's full schema must not leak to the provider.
		expect(toolNames(model.requests[0])).not.toContain("capture_screen");
	});

	it("activates a deferred tool after a matching search and exposes it next turn", async () => {
		const model = new ScriptedModel([
			// Turn 1: model loads the deferred tool via search.
			() => [
				{
					type: "tool-call-delta",
					toolCallId: "call_search",
					toolName: "search_tools",
					inputText: JSON.stringify({ query: "take a screenshot" }),
				},
				{ type: "finish", reason: "tool-calls" },
			],
			// Turn 2: deferred tool is now available; call it.
			(request) => {
				expect(toolNames(request)).toContain("capture_screen");
				return [
					{
						type: "tool-call-delta",
						toolCallId: "call_shot",
						toolName: "capture_screen",
						inputText: "{}",
					},
					{ type: "finish", reason: "tool-calls" },
				];
			},
			// Turn 3: finish. Search tool disappears once nothing is left to load.
			(request) => {
				expect(toolNames(request)).toEqual(["capture_screen", "echo"]);
				return [
					{ type: "text-delta", text: "captured" },
					{ type: "finish", reason: "stop" },
				];
			},
		]);
		const runtime = new AgentRuntime({
			model,
			tools: [normalTool, deferredTool],
		});

		const result = await runtime.run("screenshot please");

		expect(result.status).toBe("completed");
		expect(result.outputText).toBe("captured");

		const searchResult = result.messages
			.flatMap((message) => message.content)
			.find(
				(part) =>
					part.type === "tool-result" && part.toolName === "search_tools",
			);
		expect(searchResult).toBeDefined();
		expect(JSON.stringify(searchResult)).toContain("capture_screen");
	});

	it("returns the remaining catalog when a search finds no match", async () => {
		const model = new ScriptedModel([
			() => [
				{
					type: "tool-call-delta",
					toolCallId: "call_search",
					toolName: "search_tools",
					inputText: JSON.stringify({ query: "quantum teleporter" }),
				},
				{ type: "finish", reason: "tool-calls" },
			],
			(request) => {
				// Nothing activated, so the deferred tool stays hidden.
				expect(toolNames(request)).not.toContain("capture_screen");
				return [
					{ type: "text-delta", text: "ok" },
					{ type: "finish", reason: "stop" },
				];
			},
		]);
		const runtime = new AgentRuntime({
			model,
			tools: [normalTool, deferredTool],
		});

		const result = await runtime.run("do something");
		const searchResult = result.messages
			.flatMap((message) => message.content)
			.find(
				(part) =>
					part.type === "tool-result" && part.toolName === "search_tools",
			);
		expect(JSON.stringify(searchResult)).toContain("capture_screen");
	});

	it("sends deferred tools in full when the feature is disabled", async () => {
		const model = new ScriptedModel([
			() => [
				{ type: "text-delta", text: "done" },
				{ type: "finish", reason: "stop" },
			],
		]);
		const runtime = new AgentRuntime({
			model,
			tools: [normalTool, deferredTool],
			deferredTools: { enabled: false },
		});

		await runtime.run("hi");

		expect(toolNames(model.requests[0])).toEqual(["capture_screen", "echo"]);
		expect(toolNames(model.requests[0])).not.toContain("search_tools");
	});

	it("does not register a search tool when no tools are deferred", async () => {
		const model = new ScriptedModel([
			() => [
				{ type: "text-delta", text: "done" },
				{ type: "finish", reason: "stop" },
			],
		]);
		const runtime = new AgentRuntime({ model, tools: [normalTool] });

		await runtime.run("hi");

		expect(toolNames(model.requests[0])).toEqual(["echo"]);
	});
});
