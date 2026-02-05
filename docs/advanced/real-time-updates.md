# Real-Time Graph Updates

Keep your knowledge graph automatically synchronized with code changes during active development.

## What It Does

- Watches your repository for file changes (create, modify, delete)
- Automatically updates the knowledge graph in real-time
- Maintains consistency by recalculating all function call relationships
- Filters out irrelevant files (`.git`, `node_modules`, etc.)

---

## Basic Usage

Run the realtime updater in a separate terminal:

```bash
# Using Python directly
python realtime_updater.py /path/to/your/repo

# Or using the Makefile
make watch REPO_PATH=/path/to/your/repo
```

---

## With Custom Memgraph Settings

```bash
# Python
python realtime_updater.py /path/to/your/repo --host localhost --port 7687 --batch-size 1000

# Makefile
make watch REPO_PATH=/path/to/your/repo HOST=localhost PORT=7687 BATCH_SIZE=1000
```

---

## Multi-Terminal Workflow

```bash
# Terminal 1: Start the realtime updater
python realtime_updater.py ~/my-project

# Terminal 2: Run the AI assistant
graph-code start --repo-path ~/my-project

# Terminal 3: Edit your code
# The graph updates automatically as you save files
```

---

## What Gets Watched

The updater monitors:
- All files in supported languages (Python, JavaScript, TypeScript, Rust, etc.)
- File creation, modification, and deletion events
- Directory structure changes

Automatically ignored:
- `.git` directory
- `node_modules`
- Virtual environments (`.venv`, `venv`)
- Build artifacts (`dist`, `build`, `target`)
- Cache directories

---

## Performance Considerations

- Initial full parse required before real-time updates
- Updates are incremental for better performance
- Large file changes may take a few seconds to process
- Graph consistency is maintained automatically

---

## Troubleshooting

**Issue**: Updates not appearing

**Solution**: Ensure Memgraph is running and the initial parse completed successfully

**Issue**: High CPU usage

**Solution**: Reduce watch frequency or exclude large directories

---

## Related Documentation

- **[Basic Usage](../usage/basic-usage.md)** - Getting started with Graph-Code
- **[Graph Export](./graph-export.md)** - Export graph for analysis
