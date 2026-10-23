import { defineRailway, github, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "worker";
export default defineRailway(() => {
  const worker = service("worker", {
    source: github("NIC619/tem-operator-bot", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "NIXPACKS" },
    start: "python main.py",
    replicas: { "asia-southeast1-eqsg3a": 1 },
  });

  return project("TEM assistant", {
    variables: { managed: false },
    resources: [worker],
  });
});
