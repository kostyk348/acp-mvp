// Deploy script for AgentEscrow
const { ethers } = require("hardhat");

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log(`Deploying AgentEscrow from ${deployer.address}`);

  const AgentEscrow = await ethers.getContractFactory("AgentEscrow");
  const escrow = await AgentEscrow.deploy();
  await escrow.waitForDeployment();
  const address = await escrow.getAddress();

  console.log(`AgentEscrow deployed to: ${address}`);

  // Write address to a file for the ACP orchestrator to use
  const fs = require("fs");
  const path = require("path");
  const out = path.join(__dirname, "..", "deployed.json");
  fs.writeFileSync(out, JSON.stringify({
    contract: address,
    rpc_url: process.env.RPC_URL || "http://localhost:8545",
    deployed_at: new Date().toISOString(),
    network: process.env.HARDHAT_NETWORK || "localhost",
  }, null, 2));
  console.log(`Deployment info written to ${out}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});