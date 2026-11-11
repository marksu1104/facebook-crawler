// Read only metadata already delivered in application/json script elements.
// Each record is its own source; no visible text is paired to cached metadata.
function scanPostMetadata(expectedGroupURL, responsePayloads = []) {
  const MAX_BYTES = 20 * 1024 * 1024;
  const MAX_OBJECTS = 150000;
  const MAX_DEPTH = 70;
  const hosts = new Set(['facebook.com', 'www.facebook.com', 'm.facebook.com']);
  let expected;
  try { expected = new URL(expectedGroupURL); } catch { return {records:[], limit_exceeded:false}; }
  const expectedMatch = expected.pathname.match(/^\/groups\/(\d+)\/?$/);
  if (expected.protocol !== 'https:' || !hosts.has(expected.hostname) || expected.username || expected.password || (expected.port && expected.port !== '443') || !expectedMatch) return {records:[], limit_exceeded:false};
  const groupId = expectedMatch[1];
  let totalBytes = 0, objectCount = 0;
  const records = [];
  const storyTimes = new Map();
  const conflictingTimes = new Set();

  function publicationTime(raw) {
    const numeric = typeof raw === 'number' || (typeof raw === 'string' && /^\d+(?:\.\d+)?$/.test(raw)) ? Number(raw) : NaN;
    const date = new Date(numeric >= 1000000000000 ? numeric : numeric * 1000);
    return Number.isFinite(numeric) && date.getUTCFullYear() >= 2004 && date.getUTCFullYear() <= 2100 ? date.toISOString() : '';
  }

  function inspectTime(value) {
    if (!Object.prototype.hasOwnProperty.call(value, 'creation_time')) return;
    if (Array.isArray(value) || typeof value.id !== 'string' || !value.id || value.id.length > 2048) return;
    if (value.__typename && value.__typename !== 'Story') return;
    const time = publicationTime(value.creation_time);
    if (!time || conflictingTimes.has(value.id)) return;
    if (storyTimes.has(value.id) && storyTimes.get(value.id) !== time) {
      storyTimes.delete(value.id);
      conflictingTimes.add(value.id);
    } else storyTimes.set(value.id, time);
  }

  function withinByteBudget(text) {
    if (text.length > MAX_BYTES - totalBytes) return false;
    for (let index = 0; index < text.length; index++) {
      const code = text.charCodeAt(index);
      if (code <= 0x7f) totalBytes++;
      else if (code <= 0x7ff) totalBytes += 2;
      else if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length && text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) { totalBytes += 4; index++; }
      else totalBytes += 3;
      if (totalBytes > MAX_BYTES) return false;
    }
    return true;
  }

  function canonicalUrl(value, postId) {
    if (typeof value !== 'string') return '';
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' || !hosts.has(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) return '';
      if ([...url.searchParams.keys()].some(key => ['comment_id', 'reply_comment_id'].includes(key.toLowerCase()))) return '';
      const match = url.pathname.match(/^\/groups\/(\d+)\/(posts|permalink)\/(\d+)\/?$/);
      if (!match || match[1] !== groupId || match[3] !== postId) return '';
      // Facebook emits both routes for the exact same group/post identity.
      return 'https://www.facebook.com/groups/' + groupId + '/posts/' + postId + '/';
    } catch { return ''; }
  }

  function inspect(value) {
    if (Array.isArray(value) || typeof value.post_id !== 'string' || !/^\d+$/.test(value.post_id)) return;
    if (value.__typename && value.__typename !== 'Story') return;
    if (value.is_published === false || value.is_pending === true || value.is_pending_approval === true || value.is_deleted === true || value.is_draft === true) return;
    if (!value.target_group || value.target_group.id !== groupId || !value.message || typeof value.message.text !== 'string' || !value.message.text.trim()) return;
    if (!Array.isArray(value.actors)) return;
    const actors = value.actors.map(actor => actor && typeof actor.name === 'string' ? actor.name : '').filter(name => name.trim());
    if (!actors.length) return;
    let url = '';
    for (const field of ['permalink_url', 'wwwURL', 'url']) {
      url = canonicalUrl(value[field], value.post_id);
      if (url) break;
    }
    if (!url) return;
    const timestampStory = value.comet_sections?.timestamp?.story;
    const sameTimestampStory = timestampStory && (timestampStory.post_id === value.post_id || (typeof value.id === 'string' && timestampStory.id === value.id));
    const rawTime = value.creation_time ?? (sameTimestampStory ? timestampStory.creation_time : null);
    const published = publicationTime(rawTime);
    records.push({story_id:typeof value.id === 'string' ? value.id : '', post_id:value.post_id, group_id:groupId, text:value.message.text, actors, url, published_at:published});
  }

  const sources = [...document.querySelectorAll('script[type="application/json"]')]
    .map(script => ({raw:script.textContent || '', fromResponse:false}));
  for (const raw of responsePayloads) {
    if (typeof raw === 'string') sources.push({raw, fromResponse:true});
  }

  for (const source of sources) {
    const raw = source.raw;
    if (!withinByteBudget(raw)) return {records:[], limit_exceeded:true, limit_reason:'bytes'};
    const clean = raw.replace(/^\s*for\s*\(\s*;;\s*\)\s*;/, '').trim();
    let payloads;
    try {
      payloads = [JSON.parse(clean)];
    } catch {
      // Streaming feed responses can contain one JSON object per line.
      payloads = [];
      if (source.fromResponse) {
        for (const line of clean.split(/\r?\n/)) {
          try { payloads.push(JSON.parse(line)); } catch { /* Ignore incomplete chunks. */ }
        }
      }
    }
    for (const parsed of payloads) {
      const patchPath = Array.isArray(parsed?.path) ? parsed.path : [];
      if (patchPath.includes('attached_story')) continue;
      const feedPatch = patchPath.some(key => key === 'group_feed' || key === 'group_feed_connection');
      // Keep the walk stack bounded by depth, including streaming feed patches.
      const stack = [{
        value:parsed, depth:0, entered:false, index:0, keys:null,
        inFeed:!source.fromResponse || feedPatch
      }];
      while (stack.length) {
        const frame = stack[stack.length - 1];
        if (!frame.entered) {
          if (frame.depth > MAX_DEPTH) return {records:[], limit_exceeded:true, limit_reason:'depth'};
          if (!frame.value || typeof frame.value !== 'object') { stack.pop(); continue; }
          objectCount++;
          if (objectCount > MAX_OBJECTS) return {records:[], limit_exceeded:true, limit_reason:'objects'};
          inspectTime(frame.value);
          if (frame.inFeed) inspect(frame.value);
          frame.entered = true;
          if (!Array.isArray(frame.value)) frame.keys = Object.keys(frame.value);
        }
        const length = frame.keys ? frame.keys.length : frame.value.length;
        if (frame.index >= length) { stack.pop(); continue; }
        const key = frame.keys ? frame.keys[frame.index++] : frame.index++;
        // Attached shares have their own identity and are not another feed post.
        if (key === 'attached_story') continue;
        const inFeed = frame.inFeed || key === 'group_feed' || key === 'group_feed_connection';
        stack.push({
          value:frame.value[key], depth:frame.depth + 1, entered:false,
          index:0, keys:null, inFeed
        });
      }
    }
  }
  for (const record of records) {
    // Timestamp and body can be separate GraphQL fragments of one global Story ID.
    // Post text, actors, group and URL still belong to the original verified record.
    if (conflictingTimes.has(record.story_id)) {
      record.published_at = '';
      record.timestamp_conflict = true;
    }
    else record.published_at = storyTimes.get(record.story_id) || record.published_at;
  }
  return {records, limit_exceeded:false};
}

function extractLoadedPostMetadata(expectedGroupURL, responsePayloads = []) {
  const scanned = scanPostMetadata(expectedGroupURL, responsePayloads);
  if (scanned.limit_exceeded) return [];
  const posts = new Map();
  const conflicts = new Set();
  const timeConflicts = new Set();
  for (const record of scanned.records) {
    if (conflicts.has(record.post_id)) continue;
    const author = record.actors.join('、');
    if (record.timestamp_conflict) timeConflicts.add(record.post_id);
    const previous = posts.get(record.post_id);
    if (previous && (previous.content !== record.text || previous.url !== record.url || previous.author !== author)) {
      posts.delete(record.post_id);
      conflicts.add(record.post_id);
      continue;
    }
    if (!previous) {
      posts.set(record.post_id, {author, content:record.text, posted_at:'', url:record.url, post_id:record.post_id, published_at:record.published_at});
    } else if (!timeConflicts.has(record.post_id) && record.published_at) {
      if (previous.published_at && previous.published_at !== record.published_at) {
        previous.published_at = '';
        timeConflicts.add(record.post_id);
      } else previous.published_at = record.published_at;
    }
  }
  for (const post of posts.values()) {
    if (timeConflicts.has(post.post_id)) {
      post.published_at = '';
      post.timestamp_conflict = true;
    }
  }
  return [...posts.values()];
}
