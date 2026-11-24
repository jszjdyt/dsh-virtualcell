## Virtual-cell modelling
Single-cell expression profiles (2,000 cells × 1,000 genes) were used to construct a virtual-cell state space. 
After quality control, counts were library-size normalised, log1p-transformed, and restricted to 999 highly variable genes. 
A principal-component latent space was computed and cells were clustered; a k-nearest-neighbour graph was used for state transition estimation. 
A gene regulatory network was inferred by fitting per-state ridge regressions of candidate transcription factor-target pairs derived from co-expression priors (18208 directed edges, 147 transcription factors). 
In silico perturbations were simulated by clamping the perturbed gene's expression and propagating the resulting expression change through the inferred network for several iterations; the propagated change was projected into the latent space to estimate the shift of each cell state. 
All results are model-based predictions and require experimental validation.