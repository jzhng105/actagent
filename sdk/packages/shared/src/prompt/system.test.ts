import { describe, expect, it } from "vitest";
import { buildClineSystemPrompt } from "./cline";
import {
	DEFAULT_CLINE_SYSTEM_PROMPT,
	YOLO_CLINE_SYSTEM_PROMPT,
} from "./system";

// The template placeholders are the contract between the raw prompt strings and
// buildClineSystemPrompt(). If one is renamed or dropped without updating the
// builder, the substitution silently leaves a literal "{{...}}" in the prompt.
const REQUIRED_PLACEHOLDERS = [
	"{{PLATFORM_NAME}}",
	"{{CURRENT_DATE}}",
	"{{IDE_NAME}}",
	"{{CWD}}",
	"{{CLINE_RULES}}",
	"{{CLINE_METADATA}}",
];

describe("system prompt templates", () => {
	for (const [name, prompt] of [
		["DEFAULT", DEFAULT_CLINE_SYSTEM_PROMPT],
		["YOLO", YOLO_CLINE_SYSTEM_PROMPT],
	] as const) {
		it(`${name} prompt contains every required placeholder`, () => {
			for (const placeholder of REQUIRED_PLACEHOLDERS) {
				expect(prompt).toContain(placeholder);
			}
		});
	}

	it("YOLO prompt keeps the submit_and_exit completion contract", () => {
		expect(YOLO_CLINE_SYSTEM_PROMPT).toContain("submit_and_exit");
	});

	it("encodes the absorbed design principles", () => {
		// Conciseness, convention-matching, verification, and honest reporting are
		// the load-bearing behaviors; assert they survive future edits.
		const lower = DEFAULT_CLINE_SYSTEM_PROMPT.toLowerCase();
		expect(lower).toContain("concise");
		expect(lower).toContain("convention");
		expect(lower).toContain("verify");
		expect(lower).toContain("parallel");
		expect(lower).toContain("file_path:line_number");
	});
});

describe("buildClineSystemPrompt substitution", () => {
	it("resolves every placeholder for a Cline provider", () => {
		const prompt = buildClineSystemPrompt({
			providerId: "cline",
			platform: "darwin",
			ide: "VS Code",
			workspaceRoot: "/home/user/project",
			rules: "# Custom rule",
			metadata: undefined,
		});

		expect(prompt).not.toMatch(/\{\{.*?\}\}/);
		expect(prompt).toContain("darwin");
		expect(prompt).toContain("VS Code");
		expect(prompt).toContain("/home/user/project");
		expect(prompt).toContain("# Custom rule");
	});

	it("leaves no unresolved placeholders for the yolo mode", () => {
		const prompt = buildClineSystemPrompt({
			providerId: "cline",
			mode: "yolo",
			platform: "linux",
			ide: "Terminal",
			workspaceRoot: "/repo",
		});

		expect(prompt).not.toMatch(/\{\{.*?\}\}/);
		expect(prompt).toContain("submit_and_exit");
	});
});
