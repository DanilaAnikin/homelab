import Redis from "ioredis";

const REDIS_URL = process.env.REDIS_URL || "redis://localhost:6379";

let redis: Redis | null = null;

export function getRedis(): Redis {
  if (!redis) {
    redis = new Redis(REDIS_URL, {
      maxRetriesPerRequest: null,
      lazyConnect: false,
    });
    redis.on("error", (err) => {
      console.error("[redis] Connection error:", err.message);
    });
    console.log("[redis] Connected");
  }
  return redis;
}

export function getRedisConnection() {
  return new Redis(REDIS_URL, {
    maxRetriesPerRequest: null,
    lazyConnect: false,
  });
}

export { REDIS_URL };

export function closeRedis(): void {
  if (redis) {
    redis.disconnect();
    redis = null;
  }
}
