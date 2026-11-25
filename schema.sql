PRAGMA foreign_keys=ON;
-- all times are unix epoch seconds (UTC) so expiry is a plain integer comparison
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL DEFAULT 'consumer' CHECK(role IN('consumer','merchant','rider')),
  created_at INTEGER NOT NULL DEFAULT(strftime('%s','now')));
CREATE TABLE IF NOT EXISTS merchants(            -- SaaS tier lives here
  id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
  shop_name TEXT NOT NULL, area TEXT, whatsapp TEXT,
  plan TEXT NOT NULL DEFAULT 'free' CHECK(plan IN('free','pro','business')),
  plan_expires_at INTEGER);
CREATE TABLE IF NOT EXISTS ads(
  id INTEGER PRIMARY KEY, merchant_id INTEGER NOT NULL REFERENCES merchants(id),
  title TEXT NOT NULL, description TEXT, price_ugx INTEGER NOT NULL, stock INTEGER DEFAULT 1,
  video_url TEXT, poster_url TEXT, duration_hours INTEGER NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,   -- created_at + duration
  pinned_until INTEGER NOT NULL DEFAULT 0,                    -- boost/pin fee
  sponsored_until INTEGER NOT NULL DEFAULT 0,                 -- sponsored flash deal slot
  views INTEGER NOT NULL DEFAULT 0, taps INTEGER NOT NULL DEFAULT 0);  -- analytics (pro tier)
CREATE INDEX IF NOT EXISTS idx_ads_active ON ads(expires_at, sponsored_until, pinned_until);
CREATE TABLE IF NOT EXISTS boosts(               -- every micro-fee purchase
  id INTEGER PRIMARY KEY, ad_id INTEGER NOT NULL REFERENCES ads(id),
  kind TEXT NOT NULL CHECK(kind IN('extend','pin','sponsor')), hours INTEGER NOT NULL,
  fee_ugx INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY, ad_id INTEGER NOT NULL REFERENCES ads(id),
  buyer_id INTEGER NOT NULL REFERENCES users(id), merchant_id INTEGER NOT NULL REFERENCES merchants(id),
  ad_title TEXT, UNIQUE(ad_id, buyer_id));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id),
  sender_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders(               -- ad snapshot kept: the ad may expire, the order must not
  id INTEGER PRIMARY KEY, ad_id INTEGER REFERENCES ads(id), ad_title TEXT NOT NULL,
  buyer_id INTEGER NOT NULL REFERENCES users(id), merchant_id INTEGER NOT NULL REFERENCES merchants(id),
  qty INTEGER NOT NULL DEFAULT 1, item_total_ugx INTEGER NOT NULL,
  service_fee_ugx INTEGER NOT NULL,              -- platform transaction cut
  delivery_fee_ugx INTEGER NOT NULL, total_ugx INTEGER NOT NULL, address TEXT,
  status TEXT NOT NULL DEFAULT 'placed' CHECK(status IN('placed','confirmed','preparing','out_for_delivery','delivered','cancelled')),
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_events(         -- live tracking timeline
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  status TEXT NOT NULL, note TEXT, created_at INTEGER NOT NULL);
