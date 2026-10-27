import { request, ApiError } from './client.js';
import { TIMING } from '../config.js';

export function buildChatBody({ question, paperIds = [], topK, yearMin, yearMax }) {
  const body = { question };
  if (paperIds?.length) body.paper_ids = paperIds;
  if (Number.isInteger(topK)) body.top_k = topK;
  if (Number.isInteger(yearMin)) body.year_min = yearMin;
  if (Number.isInteger(yearMax)) body.year_max = yearMax;
  return body;
}

const str = (v) => (typeof v === 'string' && v ? v : null);
export function normalizeCitation(c = {}) {
  return {
    id: String(c.id ?? ''), paperId: str(c.paper_id), title: str(c.title) ?? 'Untitled paper',
    authors: Array.isArray(c.authors) ? c.authors.filter((a) => typeof a === 'string') : [],
    pageNumber: Number.isInteger(c.page_number) ? c.page_number : null, section: str(c.section),
    doi: str(c.doi), sourceUrl: str(c.source_url),
    relevanceScore: typeof c.relevance_score === 'number' ? c.relevance_score : null, excerpt: str(c.excerpt),
  };
}

export function normalizeChatResponse(raw) {
  if (!raw || typeof raw !== 'object' || typeof raw.answer !== 'string') {
    throw new ApiError('The server returned an unexpected chat response.', { kind: 'invalid_response' });
  }
  return {
    answer: raw.answer,
    citations: Array.isArray(raw.citations) ? raw.citations.map(normalizeCitation).filter((c) => c.id) : [],
    retrievedChunks: Number.isInteger(raw.retrieved_chunks) ? raw.retrieved_chunks : 0,
    model: str(raw.model), insufficientEvidence: raw.insufficient_evidence === true,
    warnings: Array.isArray(raw.warnings) ? raw.warnings.filter((w) => typeof w === 'string') : [],
  };
}

export async function askQuestion(params, { signal, baseUrl, fetchImpl } = {}) {
  const raw = await request('/api/chat', { method: 'POST', json: buildChatBody(params), timeoutMs: TIMING.chatTimeoutMs, signal, baseUrl, fetchImpl });
  return normalizeChatResponse(raw);
}
