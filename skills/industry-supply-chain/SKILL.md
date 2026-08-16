# Supply Chain Graph Skill

Use `skill_supply_chain_graph` to query Neo4j subgraph or procurement chain table.

## Parameters

- `mode`: `graph` (default) or `table`
- `center`: company UUID for ego-network
- `industry_code`: industry code e.g. `A01`
- `depth`: 1-4 hops for graph mode
- `domain`: business domain for table mode

## UI

Open `/insights/supply-chain` for Cytoscape visualization.
