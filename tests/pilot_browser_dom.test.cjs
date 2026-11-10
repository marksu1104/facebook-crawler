const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const extractor = fs.readFileSync(path.resolve(__dirname,'../app/post_extractor.js'),'utf8');
const group = 'https://www.facebook.com/groups/123456';
const link = (id, text='時間', extra='') => `<a href="${group}/posts/${id}/?tracking=ignored" ${extra}>${text}</a>`;
const message = text => `<div data-ad-preview="message">${text}</div>`;

test('a comment-targeting header URL is not accepted as a post permalink',()=>{
  const posts=extract(`<div role="article"><h3><a href="/a">作者</a></h3><a href="${group}/posts/111/?comment_id=77">留言時間</a>${message('貼文內容')}</div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
});

function extract(markup, expected=group) {
  const dom = new JSDOM(markup,{url:group,runScripts:'outside-only'});
  const {window} = dom;
  // JSDOM has no rendered layout or native innerText. Model the visible-text
  // behavior used by the live extractor, including hidden CSS descendants.
  function rendered(node) {
    if (node.nodeType === window.Node.TEXT_NODE) return node.nodeValue;
    if (node.nodeType !== window.Node.ELEMENT_NODE) return '';
    const style = window.getComputedStyle(node);
    if (node.hidden || node.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') return '';
    return [...node.childNodes].map(rendered).join('');
  }
  Object.defineProperty(window.HTMLElement.prototype,'innerText',{get(){return rendered(this);}});
  try {
    return JSON.parse(JSON.stringify(window.Function(extractor)(expected)));
  } finally { window.close(); }
}

test('roleless stories in a feed get their own headers without article assumptions',()=>{
  const posts=extract(`<div role="feed"><div class="story"><div><h3><a href="/author-a">甲</a></h3>${link('p1','2分鐘','aria-label="今天 10:00"')}</div><div><div>${message('甲的臨打貼文')}</div></div></div><div class="story"><div><h3><a href="/author-b">乙</a></h3>${link('p2')}</div><div><div>${message('乙的臨打貼文')}</div></div></div></div>`);
  assert.deepEqual(posts.map(post=>({author:post.author,content:post.content,url:post.url})),[
    {author:'甲',content:'甲的臨打貼文',url:group+'/posts/p1/'},
    {author:'乙',content:'乙的臨打貼文',url:group+'/posts/p2/'}
  ]);
  assert.equal(posts[0].posted_at,'今天 10:00');
});

test('a pure outer article wrapper does not suppress nested actual story articles',()=>{
  const posts=extract(`<div role="feed"><div role="article"><div role="article"><h3><a href="/a">甲</a></h3>${link('p1')}${message('第一篇')}</div><div role="article"><h3><a href="/b">乙</a></h3>${link('p2')}${message('第二篇')}</div></div></div>`);
  assert.deepEqual(posts.map(post=>post.url),[group+'/posts/p1/',group+'/posts/p2/']);
});

test('a header outside one nested article body can be found through its pure wrapper',()=>{
  const posts=extract(`<div role="article"><h3><a href="/a">甲</a></h3>${link('p1')}<div role="article"><div>${message('唯一的貼文')}</div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].author,'甲');
  assert.equal(posts[0].url,group+'/posts/p1/');
});

test('hidden first clones and hidden headers do not win over visible stories',()=>{
  const posts=extract(`<style>.clone{display:none}</style><div role="feed"><div class="clone">${link('hidden')}${message('隱藏複本')}</div><div hidden>${message('hidden屬性')}</div><div style="visibility:hidden">${message('visibility隱藏')}</div><div aria-hidden="true">${message('aria隱藏')}</div><div><h3><a href="/visible-author">作者</a></h3><span class="clone">${link('wrong')}</span>${link('visible')}${message('看得見的貼文')}</div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].content,'看得見的貼文');
  assert.equal(posts[0].url,group+'/posts/visible/');
});

test('nested duplicate message attributes yield one rendered story',()=>{
  const posts=extract(`<div><h3><a href="/a">甲</a></h3>${link('p1')}<div data-ad-preview="message"><div data-ad-rendering-role="story_message">臨打缺人<span style="display:none">隱藏文字</span></div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].content,'臨打缺人');
  assert.equal(posts[0].url,group+'/posts/p1/');
});

test('a separate unrelated article does not hide a roleless FeedUnit story',()=>{
  const posts=extract(`<div role="article">導覽內容</div><div role="feed"><div data-pagelet="FeedUnit_1"><h3><a href="/a">甲</a></h3>${link('p1')}${message('真正的貼文')}</div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,group+'/posts/p1/');
});

test('same-group links embedded in message text cannot replace a post header',()=>{
  const posts=extract(`<div><h3><a href="/a">甲</a></h3>${link('actual')}<div data-ad-preview="message">參考另一篇 ${link('embedded')}</div></div>`);
  assert.equal(posts[0].url,group+'/posts/actual/');
});

test('a comment without its own message cannot supply a parent post permalink',()=>{
  const posts=extract(`<div role="article"><h3><a href="/a">甲</a></h3>${message('主貼文沒有原始連結')}<div role="article"><h3><a href="/commenter">留言者</a></h3>${link('comment-link')}<div>留言文字</div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'甲');
});

test('nested comment and shared story articles are not separate top-level posts',()=>{
  const posts=extract(`<div role="article"><h3><a href="/a">甲</a></h3>${link('actual')}${message('主要貼文')}<div role="article">${link('comment')}${message('留言中的訊息標記')}</div><div role="article">${link('share')}${message('分享卡片中的訊息標記')}</div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].content,'主要貼文');
  assert.equal(posts[0].url,group+'/posts/actual/');
});

test('foreign-group and profile permalinks leave the story unlinked',()=>{
  const posts=extract(`<div><h3><a href="/a">甲</a></h3><a href="https://www.facebook.com/groups/999999/posts/wrong/">外部社團</a><a href="https://www.facebook.com/a/posts/profile/">個人貼文</a>${message('沒有本社團連結')}</div>`);
  assert.equal(posts[0].url,'');
});

test('an adjacent unlinked story never borrows its neighbor header',()=>{
  const posts=extract(`<div role="feed"><div>${message('第一篇沒有連結')}</div><div><h3><a href="/b">乙</a></h3>${link('second')}${message('第二篇有連結')}</div></div>`);
  assert.equal(posts.length,2);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'');
  assert.equal(posts[1].url,group+'/posts/second/');
});

test('a body directly in a feed cannot borrow a preceding neighbor author or link',()=>{
  const posts=extract(`<div role="feed"><div><h3><a href="/a">前一篇作者</a></h3>${link('first')}${message('前一篇')}</div>${message('直接放在feed的無連結貼文')}</div>`);
  assert.equal(posts.length,2);
  assert.equal(posts[1].url,'');
  assert.equal(posts[1].author,'');
});

test('ambiguous preceding post identities are held unlinked',()=>{
  const posts=extract(`<div><h3><a href="/a">甲</a></h3>${link('one')}${link('two')}${message('無法確認哪個是原始貼文')}</div>`);
  assert.equal(posts[0].url,'');
});

test('a later heading with a post link remains unlinked rather than guessing a comment header',()=>{
  const posts=extract(`<div><div><div>${message('畫面上的 header 在 DOM 中較晚')}</div></div><div role="heading">${link('after','剛剛','aria-label="今天 11:00"')}${link('after')}</div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].posted_at,'');
});

test('the preceding local header wins over a later roleless comment heading',()=>{
  const posts=extract(`<div><h3><a href="/a">甲</a></h3>${link('before')}${message('不借用留言連結')}<div role="heading">${link('after')}</div></div>`);
  assert.equal(posts[0].url,group+'/posts/before/');
});

test('a later plain roleless comment or share link is not treated as a header',()=>{
  const posts=extract(`<div>${message('主文沒有連結')}<div><a href="/commenter">留言者</a>${link('comment-or-share')}</div></div>`);
  assert.equal(posts[0].url,'');
});

test('an empty neighbor message marker prevents borrowing its visible timestamp',()=>{
  const posts=extract(`<div role="feed"><div><div><h3><a href="/empty">空白鄰居作者</a></h3>${link('empty')}${message('')}</div><div>${message('目前無連結的貼文')}</div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].content,'目前無連結的貼文');
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'');
});

test('a hidden neighbor message marker prevents borrowing its visible timestamp',()=>{
  const posts=extract(`<div role="feed"><div><div><h3><a href="/hidden">隱藏鄰居作者</a></h3>${link('hidden-neighbor')}<div data-ad-preview="message" hidden>隱藏內容</div></div><div>${message('目前無連結的貼文')}</div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'');
});

test('the current feed child never borrows a separate child header with no message marker',()=>{
  const posts=extract(`<div role="feed"><div><h3><a href="/neighbor">鄰居</a></h3>${link('neighbor')}</div><div>${message('獨立分支沒有連結')}</div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'');
});

test('deep roleless wrappers can reach a header without entering the feed',()=>{
  let nested=message('深層貼文');
  for(let depth=0;depth<16;depth++)nested=`<div>${nested}</div>`;
  const posts=extract(`<div role="feed"><div><h3><a href="/deep">作者</a></h3>${link('deep')}${nested}</div><div>${message('隔壁沒有連結')}</div></div>`);
  assert.equal(posts.length,2);
  assert.equal(posts[0].url,group+'/posts/deep/');
  assert.equal(posts[1].url,'');
});

test('duplicate links to one post identity are accepted but invalid expected groups are not',()=>{
  const markup=`<div><h3><a href="/a">甲</a></h3>${link('p1')}<a href="https://m.facebook.com/groups/123456/permalink/p1/">同一篇</a>${message('內容')}</div>`;
  assert.equal(extract(markup)[0].url,group+'/posts/p1/');
  assert.deepEqual(extract(markup,'https://evil.test/groups/123456'),[]);
  assert.deepEqual(extract(markup,'https://www.facebook.com:8443/groups/123456'),[]);
});

test('an image-only preceding story cannot supply an unlinked story timestamp',()=>{
  const posts=extract(`<div role="feed"><div><div><h3><a href="/image-author">照片作者</a></h3>${link('image')}<img alt="照片" src="/photo.jpg"></div><div><h3><a href="/current">目前作者</a></h3><a href="${group}/">未具ID的時間</a>${message('目前的文字貼文')}</div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'目前作者');
});

test('image-only neighbor is not borrowed even when the current body has no heading',()=>{
  const posts=extract(`<div role="feed"><div><div><h3><a href="/image-author">照片作者</a></h3>${link('image')}<img alt="照片" src="/photo.jpg"></div><div>${message('沒有標題的文字貼文')}</div></div></div>`);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,'');
  assert.equal(posts[0].author,'');
});

test('nearest preceding author heading excludes an earlier header in the same container',()=>{
  const posts=extract(`<div><h3><a href="/earlier">前一位作者</a></h3>${link('earlier')}<h3><a href="/current">目前作者</a></h3>${link('current')}${message('目前貼文')}</div>`);
  assert.equal(posts[0].author,'目前作者');
  assert.equal(posts[0].url,group+'/posts/current/');
});
