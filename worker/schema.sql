CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  excerpt TEXT,
  image TEXT,
  published TEXT,
  writer TEXT NOT NULL,
  category TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS writers (
  name TEXT PRIMARY KEY,
  url TEXT, feed_url TEXT, avatar TEXT, bio TEXT, categories TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_cat ON articles(category);
CREATE INDEX IF NOT EXISTS idx_pub ON articles(published DESC);
CREATE INDEX IF NOT EXISTS idx_writer ON articles(writer);
