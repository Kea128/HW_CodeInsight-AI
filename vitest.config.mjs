import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["desktop-ui/**/*.test.js"],
    pool: "threads",
    maxWorkers: 1,
  },
});
