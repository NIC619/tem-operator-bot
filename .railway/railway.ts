import { defineRailway, github, project, service, volume } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "worker";
export default defineRailway(() => {
  // Holds the SQLite DB, Gmail OAuth files, config.yaml and reviewers.md.
  const data = volume("worker-volume", { sizeMB: 500, region: "asia-southeast1-eqsg3a" });

  const worker = service("worker", {
    source: github("NIC619/tem-operator-bot", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "NIXPACKS" },
    start: "python main.py",
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    volumeMounts: { "/data": data },
  });

  return project("TEM assistant", {
    variables: { managed: false },
    resources: [worker, data],
  });
});
