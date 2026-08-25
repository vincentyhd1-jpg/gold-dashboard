import fs from 'fs';
import { chromium } from 'playwright';

export class BrowserEnvironmentError extends Error {
  constructor(stage, executablePath, cause) {
    const code = cause?.code || 'UNKNOWN';
    const original = cause?.message || String(cause);
    super(
      `Playwright chromium ${stage} failed; ` +
      `executablePath=${JSON.stringify(executablePath)}; code=${code}; ${original}`,
      { cause },
    );
    this.name = 'BrowserEnvironmentError';
    this.browserType = 'chromium';
    this.stage = stage;
    this.executablePath = executablePath;
    this.code = code;
  }
}

/**
 * Launch the full Chromium executable belonging to this installed Playwright.
 *
 * Playwright's default headless launch may target a separately installed
 * headless-shell package. This project has the full Chromium package, so use
 * Playwright's own executablePath() result explicitly. Do not scan caches,
 * choose another revision, download a browser, or fall back to system Chrome.
 *
 * The second argument is test-only dependency injection for the infrastructure
 * guard. Production verify scripts must call launchChromium(options) only.
 */
export async function launchChromium(options = {}, testOnly = {}) {
  const browserType = testOnly.browserType || chromium;
  const accessSync = testOnly.accessSync || fs.accessSync;
  let resolvedExecutable = testOnly.executablePath;

  if (!resolvedExecutable) {
    try {
      resolvedExecutable = browserType.executablePath();
    } catch (error) {
      throw new BrowserEnvironmentError('executable resolution', '<unresolved>', error);
    }
  }

  try {
    accessSync(resolvedExecutable, fs.constants.R_OK | fs.constants.X_OK);
  } catch (error) {
    throw new BrowserEnvironmentError('executable access', resolvedExecutable, error);
  }

  try {
    return await browserType.launch({
      headless: true,
      ...options,
      executablePath: resolvedExecutable,
    });
  } catch (error) {
    throw new BrowserEnvironmentError('launch', resolvedExecutable, error);
  }
}
