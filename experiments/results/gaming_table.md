# Gaming table (200 jobs per archetype, seed 7)

Parameters (defaults, not tuned): p=1000, rho=0.5, gamma=0.6, sigma=0; ex-post k varies. Credits are floats here (no ledger rounding).

- Noise model (assumption): measured energy and intensities = true x (1 + N(0, sd)) with sd = 10% for energy and 10% for intensity, and the reported uncertainty equals the true sd (honest error bars). Measurements of the submit-time and run-time intensity are independent, except for always_immediate, where the counterfactual IS the actual run (one shared measurement).
- Ex-ante baseline (SIMPLIFIED STRAWMAN, not a real scheme): decided at submit time with no measurement. fee = p x declared_E x forecast(i_run); rebate = rho x p x declared_E x (forecast(i_submit) - forecast(i_run)), applied as written (a negative value acts as a charge), with no dead zone and no cap. declared_E = true energy.
- noise_chaser uses a flat 400 g/kWh curve by definition; the other archetypes use the simulated curve (seed 42, UTC) and the Phase 3 forecast with its default error model.

| archetype | rule | mean net / job | share rebated | rebates minted | min net | jobs with net < 0 |
|---|---|---:|---:|---:|---:|---:|
| honest_deferrer | ex-ante | 172.9075 | 55.5% | 3691.9266 | -18.056280 | 2 |
| honest_deferrer | ex-post k=0 | 173.1037 | 73.5% | 4052.1625 | 12.164921 | 0 |
| honest_deferrer | ex-post k=1 | 181.9435 | 46.5% | 2284.2090 | 14.831382 | 0 |
| honest_deferrer | ex-post k=2 | 187.2212 | 30.0% | 1228.6565 | 17.497843 | 0 |
| always_immediate | ex-ante | 217.5040 | 0.0% | 0.0000 | 24.181867 | 0 |
| always_immediate | ex-post k=0 | 217.0043 | 0.0% | 0.0000 | 23.122038 | 0 |
| always_immediate | ex-post k=1 | 217.0043 | 0.0% | 0.0000 | 23.122038 | 0 |
| always_immediate | ex-post k=2 | 217.0043 | 0.0% | 0.0000 | 23.122038 | 0 |
| junk_farmer | ex-ante | 0.9565 | 55.5% | 15.5635 | 0.094268 | 0 |
| junk_farmer | ex-post k=0 | 0.9229 | 67.5% | 18.1562 | 0.203183 | 0 |
| junk_farmer | ex-post k=1 | 0.9668 | 42.5% | 9.3806 | 0.203183 | 0 |
| junk_farmer | ex-post k=2 | 0.9923 | 25.0% | 4.2721 | 0.250590 | 0 |
| noise_chaser | ex-ante | 217.9545 | 39.5% | 752.0619 | 39.257675 | 0 |
| noise_chaser | ex-post k=0 | 205.6350 | 49.5% | 1215.0518 | 33.332595 | 0 |
| noise_chaser | ex-post k=1 | 210.4271 | 17.5% | 256.6241 | 36.213460 | 0 |
| noise_chaser | ex-post k=2 | 211.5264 | 1.5% | 36.7644 | 37.345213 | 0 |

Reading: Under the simplified ex-ante strawman some jobs were paid to run (net below zero): 2 honest_deferrer job(s), lowest -18.0563. Across all ex-post runs the lowest net of any job was 0.203183 credits, so no job ever earned credits by running. On the flat curve, where the true gain is zero, the noise chaser received a rebate on 50% of jobs with k=0 and on 2% with k=2.

CSV saved to /home/user/greensettle/experiments/results/gaming_table.csv
