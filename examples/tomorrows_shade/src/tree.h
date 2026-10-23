#ifndef TREE_H
#define TREE_H

#include "world.h"

void TreeInit(unsigned seed);
/* growth: 0 (acorn) .. 1 (old giant). leafAmt scales the canopy. */
void TreeBuild(TreeState *ts, Vector3 base, float growth, float leafAmt, float wind, float time);
Vector3 TreeSwingAnchor(Vector3 base, float growth);

#endif
