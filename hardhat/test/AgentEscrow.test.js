const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("AgentEscrow", function () {
  let escrow;
  let customer;
  let agent;
  let owner;

  beforeEach(async function () {
    [owner, customer, agent] = await ethers.getSigners();
    const AgentEscrow = await ethers.getContractFactory("AgentEscrow");
    escrow = await AgentEscrow.deploy();
    await escrow.waitForDeployment();
  });

  describe("createTask", function () {
    it("Should create a task with budget", async function () {
      const tx = await escrow.connect(customer).createTask(
        "msg-001",
        ethers.parseEther("1.0"),
        "ETH",
        Math.floor(Date.now() / 1000) + 3600
      );
      await tx.wait();

      const taskId = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["string", "uint256"],
          ["msg-001", (await ethers.getBlock("latest")).timestamp]
        )
      );
      const task = await escrow.tasks(taskId);
      expect(task.customer).to.equal(customer.address);
      expect(task.budget).to.equal(ethers.parseEther("1.0"));
      expect(task.status).to.equal(0); // Created
    });

    it("Should reject zero budget", async function () {
      await expect(
        escrow.connect(customer).createTask("msg-001", 0, "ETH", 0)
      ).to.be.revertedWith("Budget must be > 0");
    });
  });

  describe("submitResult", function () {
    it("Should allow agent to submit result", async function () {
      // Create task first
      const tx = await escrow.connect(customer).createTask(
        "msg-001",
        ethers.parseEther("1.0"),
        "ETH",
        Math.floor(Date.now() / 1000) + 3600
      );
      await tx.wait();

      // Get task ID (simplified — in real usage would track it)
      const block = await ethers.getBlock("latest");
      const taskId = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["string", "uint256"],
          ["msg-001", block.timestamp]
        )
      );

      await expect(
        escrow.connect(agent).submitResult(taskId, "0xabc123")
      ).to.emit(escrow, "ResultSubmitted").withArgs(taskId, "0xabc123");

      const task = await escrow.tasks(taskId);
      expect(task.status).to.equal(1); // Submitted
      expect(task.executionHash).to.equal("0xabc123");
    });
  });

  describe("verifyAndRelease", function () {
    it("Should release funds on success", async function () {
      const budget = ethers.parseEther("1.0");
      const tx = await escrow.connect(customer).createTask(
        "msg-001",
        budget,
        "ETH",
        Math.floor(Date.now() / 1000) + 3600,
        { value: budget }
      );
      await tx.wait();

      const block = await ethers.getBlock("latest");
      const taskId = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["string", "uint256"],
          ["msg-001", block.timestamp]
        )
      );

      await escrow.connect(agent).submitResult(taskId, "0xabc123");

      const agentBalanceBefore = await ethers.provider.getBalance(agent.address);
      const tx2 = await escrow.connect(customer).verifyAndRelease(taskId, true);
      await tx2.wait();

      const agentBalanceAfter = await ethers.provider.getBalance(agent.address);
      expect(agentBalanceAfter).to.be.gt(agentBalanceBefore);

      const task = await escrow.tasks(taskId);
      expect(task.status).to.equal(3); // Released
    });

    it("Should refund on failure", async function () {
      const budget = ethers.parseEther("1.0");
      const tx = await escrow.connect(customer).createTask(
        "msg-002",
        budget,
        "ETH",
        Math.floor(Date.now() / 1000) + 3600,
        { value: budget }
      );
      await tx.wait();

      const block = await ethers.getBlock("latest");
      const taskId = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["string", "uint256"],
          ["msg-002", block.timestamp]
        )
      );

      await escrow.connect(agent).submitResult(taskId, "0xdef456");

      const customerBalanceBefore = await ethers.provider.getBalance(customer.address);
      const tx2 = await escrow.connect(customer).verifyAndRelease(taskId, false);
      await tx2.wait();

      const customerBalanceAfter = await ethers.provider.getBalance(customer.address);
      expect(customerBalanceAfter).to.be.gt(customerBalanceBefore);

      const task = await escrow.tasks(taskId);
      expect(task.status).to.equal(4); // Refunded
    });
  });
});
