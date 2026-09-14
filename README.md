## Running locally (and adding your own repos)

The public demo runs against a fixed set of pre-indexed repos and can't
index new ones live (see "Why is live indexing disabled?" below). To
index your own repo, run this project on your own machine instead --
full control, no resource limits, no restrictions.

### Setup

1. Clone this repo and `cd` into it.
2. Install dependencies: `pip install -r requirements.txt`
3. Create a `.env` file with:

GITHUB_TOKEN=your_github_token_here

   (A fine-grained token with public-repo read access is enough --
   this is used to fetch linked issue/PR titles.)
4. Install [Ollama](https://ollama.com) and pull the model this project
   uses by default:

ollama pull llama3.2:3b

5. Start the server:

python -m uvicorn app:app --reload

   (On Windows, if you hit an OpenMP-related crash on startup, prefix
   the command with `KMP_DUPLICATE_LIB_OK=TRUE` -- a known conflict
   between numpy's and torch's bundled OpenMP runtimes, harmless to
   work around this way.)
6. Open `http://localhost:8000` in your browser.

### Adding a repo

Click **"+ Add repo"** in the top bar and paste any public GitHub URL.
This clones it, mines its git history, matches function identity across
renames, links commits to real GitHub issues/PRs, and builds semantic
embeddings -- all in the background. Expect this to take real minutes,
not seconds, for a repo with substantial history (the original httpx
index, 450 commits, took about 9 minutes on a laptop).

### Why is live indexing disabled on the public deployment?

The public demo runs on a free-tier container with 0.1 CPU and 512MB
RAM and no persistent disk -- a real indexing job would be extremely
slow at best, and anything it produced would vanish on the next
restart. Rather than offer a broken or misleading version of the
feature publicly, it's fully enabled locally and disabled on the
hosted demo, with an honest message explaining why.
