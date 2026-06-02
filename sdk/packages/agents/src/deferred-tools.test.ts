import { describe, expect, it } from "vitest";
import {
	type DeferredToolCandidate,
	describeDeferredTool,
	rankDeferredTools,
	tokenize,
} from "./deferred-tools";

const candidates: DeferredToolCandidate[] = [
	{
		name: "capture_screen",
		description: "Take a screenshot of the current display",
		searchHint: "screenshot image png",
	},
	{ name: "read_file", description: "Read the contents of a file from disk" },
	{ name: "send_email", description: "Send an email to a recipient" },
];

describe("tokenize", () => {
	it("splits on non-alphanumeric and lowercases", () => {
		expect(tokenize("Capture_Screen v2!")).toEqual(["capture", "screen", "v2"]);
	});
});

describe("rankDeferredTools", () => {
	it("matches via searchHint keywords that are absent from name/description", () => {
		const matches = rankDeferredTools(candidates, "take a screenshot");
		expect(matches[0]?.tool.name).toBe("capture_screen");
	});

	it("weighs name matches above description matches", () => {
		const matches = rankDeferredTools(candidates, "read file");
		expect(matches[0]?.tool.name).toBe("read_file");
	});

	it("returns nothing for unrelated queries", () => {
		expect(rankDeferredTools(candidates, "quantum teleporter")).toEqual([]);
	});

	it("ignores stop words so common filler does not match everything", () => {
		expect(
			rankDeferredTools(candidates, "i need a tool to use please"),
		).toEqual([]);
	});

	it("respects the activation limit", () => {
		const matches = rankDeferredTools(candidates, "file email screen", 1);
		expect(matches).toHaveLength(1);
	});

	it("supports partial-word matches", () => {
		const matches = rankDeferredTools(candidates, "screenshots");
		expect(matches[0]?.tool.name).toBe("capture_screen");
	});
});

describe("describeDeferredTool", () => {
	it("includes keywords when a search hint is present", () => {
		expect(describeDeferredTool(candidates[0])).toBe(
			"- capture_screen: Take a screenshot of the current display (keywords: screenshot image png)",
		);
	});

	it("omits the keywords suffix without a hint", () => {
		expect(describeDeferredTool(candidates[1])).toBe(
			"- read_file: Read the contents of a file from disk",
		);
	});
});
