/**
 * Pure helpers for deferred ("on-demand") tool loading.
 *
 * Tools flagged with `defer: true` are withheld from the model's initial tool
 * list. A synthetic search tool lets the model load them by query when relevant,
 * keeping the per-turn tool payload (and token cost) small for large catalogs.
 *
 * Everything here is side-effect free so it can be unit-tested without a running
 * {@link AgentRuntime}. The runtime owns activation state and tool execution.
 */

export interface DeferredToolCandidate {
	name: string;
	description: string;
	searchHint?: string;
}

export interface DeferredToolMatch<T extends DeferredToolCandidate> {
	tool: T;
	score: number;
}

export const DEFAULT_SEARCH_TOOL_NAME = "search_tools";
export const DEFAULT_MAX_ACTIVATIONS_PER_SEARCH = 10;

// Common words that carry no signal for tool matching. Dropping them avoids
// spurious matches like "the"/"a"/"to" hitting every tool description.
const STOP_WORDS = new Set([
	"a",
	"an",
	"and",
	"the",
	"to",
	"of",
	"for",
	"in",
	"on",
	"with",
	"is",
	"it",
	"or",
	"my",
	"me",
	"i",
	"need",
	"want",
	"please",
	"tool",
	"tools",
	"use",
	"using",
	"can",
	"that",
	"this",
]);

/** Split text into lowercase alphanumeric tokens. */
export function tokenize(text: string): string[] {
	return text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

function meaningfulQueryTokens(query: string): string[] {
	return [...new Set(tokenize(query))].filter(
		(token) => token.length > 1 && !STOP_WORDS.has(token),
	);
}

/**
 * Rank deferred-tool candidates against a free-text query.
 *
 * Scoring favours name matches over description/hint matches, with a small
 * partial-match fallback so e.g. "screenshot" can surface a "screen_capture"
 * tool. Returns at most `limit` matches with a positive score, best first.
 */
export function rankDeferredTools<T extends DeferredToolCandidate>(
	candidates: readonly T[],
	query: string,
	limit: number = DEFAULT_MAX_ACTIVATIONS_PER_SEARCH,
): DeferredToolMatch<T>[] {
	const queryTokens = meaningfulQueryTokens(query);
	if (queryTokens.length === 0 || limit <= 0) {
		return [];
	}

	const matches: DeferredToolMatch<T>[] = [];
	for (const tool of candidates) {
		const nameTokens = new Set(tokenize(tool.name));
		const haystackTokens = new Set([
			...nameTokens,
			...tokenize(tool.description),
			...tokenize(tool.searchHint ?? ""),
		]);

		let score = 0;
		for (const token of queryTokens) {
			if (nameTokens.has(token)) {
				score += 2;
			} else if (haystackTokens.has(token)) {
				score += 1;
			} else if (token.length >= 3) {
				// Partial-word fallback (e.g. "screenshots" ↔ "screenshot"). Require
				// both sides to be reasonably long so short fragments like "a" inside
				// "quantum" do not produce spurious matches.
				for (const candidate of haystackTokens) {
					if (candidate.length < 3) {
						continue;
					}
					if (candidate.includes(token) || token.includes(candidate)) {
						score += 0.5;
						break;
					}
				}
			}
		}

		if (score > 0) {
			matches.push({ tool, score });
		}
	}

	matches.sort(
		(a, b) => b.score - a.score || a.tool.name.localeCompare(b.tool.name),
	);
	return matches.slice(0, limit);
}

/** Render a human/LLM-readable catalog line for a deferred tool. */
export function describeDeferredTool(tool: DeferredToolCandidate): string {
	const hint = tool.searchHint ? ` (keywords: ${tool.searchHint})` : "";
	return `- ${tool.name}: ${tool.description}${hint}`;
}
