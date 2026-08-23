import fs from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'

const base = "/playlist-mirror/"
const hostname = "https://itobey.github.io"

// app/version.py is the single declared source of truth for the version, written by the
// release workflow. Read it here so the navbar and any install snippet stay current without
// a second place to remember to bump. DOCS_VERSION wins, for a docs build of a ref whose
// working tree is not the one being released.
function resolveVersion() {
    if (process.env.DOCS_VERSION) {
        return process.env.DOCS_VERSION.trim()
    }
    try {
        const here = path.dirname(fileURLToPath(import.meta.url))
        const source = fs.readFileSync(path.resolve(here, "../../app/version.py"), "utf-8")
        return source.match(/__version__\s*=\s*"(.*?)"/)?.[1] ?? ""
    } catch {
        // A docs build must never fail over a missing version - it is a label, not content.
        return ""
    }
}

const version = resolveVersion()

export default {
    // site-level options
    title: 'Playlist Mirror',
    description: 'Copy a YouTube channel or playlist into your own private, editable playlist',
    lang: 'en-US',

    base: base,
    // site level, not themeConfig: this is what enables the git timestamp collection that
    // themeConfig.lastUpdated only labels and formats. Needs fetch-depth: 0 in the workflow.
    lastUpdated: true,
    sitemap: {
        hostname: hostname + base
    },
    head: [
        ['link', {rel: 'icon', href: base + 'images/mark.svg'}],
        ['meta', {name: 'theme-color', content: '#a8301b'}],
        ['meta', {property: 'og:type', content: 'website'}],
        ['meta', {property: 'og:site_name', content: 'Playlist Mirror'}],
        ['meta', {property: 'og:title', content: 'Playlist Mirror'}],
        ['meta', {
            property: 'og:description',
            content: 'Copy a YouTube channel or playlist into your own private, editable playlist'
        }],
        ['meta', {property: 'og:url', content: hostname + base}],
        ['meta', {name: 'twitter:card', content: 'summary'}],
        ['meta', {name: 'twitter:title', content: 'Playlist Mirror'}],
        ['meta', {
            name: 'twitter:description',
            content: 'Copy a YouTube channel or playlist into your own private, editable playlist'
        }]
    ],
    themeConfig: {
        // exposed to markdown pages through useData(), so version strings stay current
        version: version,
        lastUpdated: {
            text: 'Last updated'
        },
        outline: [2, 4],
        search: {
            provider: "local"
        },
        editLink: {
            pattern: 'https://github.com/itobey/playlist-mirror/edit/master/docs/:path',
            text: 'Edit this page on GitHub'
        },
        footer: {
            message: 'Self-hosted, single user, single Docker container.',
        },
        nav: [
            {
                text: "Project",
                items: [
                    { text: "Releases", link: "https://github.com/itobey/playlist-mirror/releases" },
                    { text: "Issues", link: "https://github.com/itobey/playlist-mirror/issues" }
                ]
            },
            { text: "Helm chart", link: "https://github.com/itobey/charts/tree/master/playlist-mirror" },
            ...(version ? [{ text: "v" + version, link: "https://github.com/itobey/playlist-mirror/releases" }] : [])
        ],
        socialLinks: [
            {
                icon: {
                    svg: '<svg height="32" aria-hidden="true" viewBox="0 0 16 16" version="1.1" width="32" data-view-component="true" class="octicon octicon-mark-github v-align-middle color-fg-default"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"></path></svg>'
                },
                link: "https://github.com/itobey/playlist-mirror"
            }
        ],
        // hand-maintained: a new page needs an entry here in the same commit.
        // Links are extensionless on purpose - cleanUrls is on, so an .html link would not match the
        // canonical path and the current page would not be highlighted. Links inside markdown bodies
        // stay .md-suffixed, which is what VitePress rewrites and validates.
        sidebar: [
            {
                text: "Introduction",
                collapsed: false,
                items: [
                    { text: "What is Playlist Mirror?", link: "/introduction/" },
                    { text: "Getting started", link: "/introduction/getting-started" }
                ]
            },
            {
                text: "Details",
                collapsed: false,
                items: [
                    { text: "Configuration", link: "/details/configuration" },
                    { text: "Docker", link: "/details/docker" },
                    { text: "Helm", link: "/details/helm" },
                    { text: "Quota", link: "/details/quota" },
                    { text: "Daily runs", link: "/details/daily-runs" },
                    { text: "Notifications", link: "/details/notifications" },
                    { text: "Securing your instance", link: "/details/security" },
                    { text: "Upgrading and backups", link: "/details/upgrading" },
                    { text: "Troubleshooting", link: "/details/troubleshooting" },
                    { text: "Privacy and telemetry", link: "/details/telemetry" }
                ]
            }
        ]
    },
    markdown: {
        config(md) {
            // Write __DOCS_VERSION__ inside a code fence and it becomes the current version at
            // build time, so no page hard-codes a version that then drifts on the next release.
            // Deliberately not Vue interpolation in a ```bash-vue fence: that only resolves after
            // hydration, leaving the static HTML with an empty version.
            const fence = md.renderer.rules.fence
                ?? ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))
            md.renderer.rules.fence = (...args) => fence(...args).replaceAll('__DOCS_VERSION__', version)
        }
    },
    ignoreDeadLinks: [
        // ignore all localhost links
        /^https?:\/\/localhost/,
    ],
    cleanUrls: true
}
