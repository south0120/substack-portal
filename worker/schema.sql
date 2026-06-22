CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  excerpt TEXT,
  image TEXT,
  published TEXT,
  writer TEXT NOT NULL,
  category TEXT NOT NULL,
  is_audio INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
-- Existing D1 databases require: ALTER TABLE articles ADD COLUMN is_audio INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS writers (
  name TEXT PRIMARY KEY,
  url TEXT, feed_url TEXT, avatar TEXT, bio TEXT, categories TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  feed_url TEXT NOT NULL,
  category TEXT,
  bio TEXT,
  status TEXT DEFAULT 'pending',
  pr_url TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cat ON articles(category);
CREATE INDEX IF NOT EXISTS idx_pub ON articles(published DESC);
CREATE INDEX IF NOT EXISTS idx_writer ON articles(writer);
-- 名前変更(rename)検知のため feed_url で書き手を引けるように
CREATE INDEX IF NOT EXISTS idx_writer_feed ON writers(feed_url);
