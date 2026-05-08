# Corpus Manager

> Corpus Manager is a self-hosted cloud MCP service that turns your notes into a queryable personal knowledge base you can use from anywhere.

Corpus Manager runs on your VPS and connects Claude.ai to your synced Obsidian vault, giving you a natural-language workflow to capture ideas, process them into structured wiki knowledge, and query that knowledge later without being tied to a single device. It follows [the "second brain" LLM wiki pattern popularized by Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

## How It Works

You chat with Claude naturally and use it to capture ideas, notes, and source material as they come up. Claude will store captured ideas in an Obsidian wiki in a "raw" source folder. You can also manually add notes and ideas to the raw folder by typing them directly into Obsidian, dragging or copying files into the folder, or even capturing web clips directly into the folder using a tool like [MarkDownload](https://addons.mozilla.org/en-US/firefox/addon/markdownload/).

After capturing many ideas, you can tell Claude to process those captures and ingest them into your wiki. The raw captures will remain untouched, and Claude will read them and integrate them into an organized knowledge base in the "wiki" folder.

The wiki syncs to all of your devices, and you can view and edit it manually via Obsidian, or ask Claude questions about it via chat.

## Getting Started

The fully functional Corpus Manager system has several pieces that require manual setup. In a nutshell:

1) Set up a VPS (Recommended: DigitalOcean Droplet, 1 vCPU, 2 GB RAM, 50 GB SSD, 2 TB transfer, backups; about $15/month)
2) Install Corpus Manager and Syncthing with Docker using the included [`Dockerfile`](Dockerfile) and [`docker-compose.yml`](docker-compose.yml)
3) Create and mount an Obsidian vault on the VPS
4) Configure Syncthing so the vault syncs between VPS, Mac, and iPhone
5) Add a domain A record for your MCP subdomain, and configure TLS reverse proxy
6) Install the MCP server in Claude (or another AI agent)
7) Install the skill from the [`skill/`](skill/) directory to teach Claude how to interact with your wiki
8) Start chatting! Capture notes, process/ingest them into the wiki, and ask questions against your knowledge base from anywhere

## Documentation

For VPS installation, setup, troubleshooting, and development operations, see [`docs/setup-and-operations.md`](docs/setup-and-operations.md).

For more detailed usage instructions and practical examples, see [`docs/usage.md`](docs/usage.md).

## Bug Reports and Pull Requests

If you use this and run into any issues, please file a bug report on GitHub. Pull requests are also welcome. I'll do my best to address things and update them. However, I built this for my own personal use. I cannot promise to maintain anything. If you choose to use this, you use it at your own risk, absolutely no guarantees are made. This repo contains bugs, and Claude makes mistakes. I accept no responsibility if this destroys your wiki or creates any other problems.
