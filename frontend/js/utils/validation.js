/* Client-side validation is a convenience only; the backend remains the authority. */
export function validateQuestion(text, max = 2000) {
  const value = String(text ?? '').trim();
  if (!value) return { ok: false, error: 'Please enter a question.', value };
  if (value.length > max) return { ok: false, error: `Question is too long (${value.length}/${max} characters).`, value };
  return { ok: true, error: null, value };
}

export function validatePdf(file, maxMb = 50) {
  if (!file) return { ok: false, error: 'Choose a PDF file.' };
  if (!/\.pdf$/i.test(file.name || '')) return { ok: false, error: 'Only .pdf files are accepted.' };
  if (file.type && file.type !== 'application/pdf') return { ok: false, error: 'This file does not look like a PDF.' };
  if (!(file.size > 0)) return { ok: false, error: 'The file is empty.' };
  if (file.size > maxMb * 1024 * 1024) return { ok: false, error: `The file is larger than the ${maxMb} MB limit.` };
  return { ok: true, error: null };
}

export function safeHttpUrl(value) {
  try { const u = new URL(String(value)); return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null; } catch { return null; }
}
export function doiUrl(doi) {
  const d = String(doi ?? '').trim().replace(/^(https?:\/\/(dx\.)?doi\.org\/|doi:)/i, '');
  return /^10\.\d{4,9}\/\S+$/.test(d) ? `https://doi.org/${encodeURI(d)}` : null;
}

export function validateMetadata({ year, doi, sourceUrl } = {}) {
  const errors = {};
  if (year !== '' && year !== null && year !== undefined) {
    const n = Number(year);
    if (!Number.isInteger(n) || n < 1000 || n > 3000) errors.year = 'Enter a four-digit year.';
  }
  if (doi && !doiUrl(doi)) errors.doi = 'DOI should look like 10.1234/abcd.';
  if (sourceUrl && !safeHttpUrl(sourceUrl)) errors.sourceUrl = 'Enter a full http(s) URL.';
  return errors;
}
