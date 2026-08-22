import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vendor = resolve(root, "desktop-ui", "vendor");

await mkdir(vendor, { recursive: true });
await Promise.all([
  copyFile(
    resolve(root, "node_modules", "@xterm", "xterm", "lib", "xterm.mjs"),
    resolve(vendor, "xterm.mjs"),
  ),
  copyFile(
    resolve(root, "node_modules", "@xterm", "xterm", "css", "xterm.css"),
    resolve(vendor, "xterm.css"),
  ),
  copyFile(
    resolve(root, "node_modules", "@xterm", "addon-fit", "lib", "addon-fit.mjs"),
    resolve(vendor, "addon-fit.mjs"),
  ),
]);

console.log("Prepared local xterm assets for desktop build.");
