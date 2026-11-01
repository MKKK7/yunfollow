import { readDocument, validateUrl } from './kdocs-read.mjs';

const args = process.argv.slice(2);
if (args.length !== 4 || args[0] !== '--url' || args[2] !== '--cli') throw new Error('Expected one registered document URL and CLI path');
try {
  const snapshot = await readDocument({ url: validateUrl(args[1]), cli: args[3] });
  console.log(JSON.stringify({ source: snapshot.source, status: snapshot.status, contentPersisted: false,
    contentHash: snapshot.contentHash, completeness: snapshot.completeness,
    candidates: { shares115: snapshot.candidates.shares115, offlineLinks: snapshot.candidates.offlineLinks } }));
} catch {
  console.error('Registered document read failed; credentials and body are not logged');
  process.exitCode = 1;
}
