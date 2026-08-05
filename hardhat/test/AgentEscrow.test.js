const { expect } = require("chai");
const { ethers } = require("hardhat");

const ZERO_ADDR = "0x0000000000000000000000000000000000000000";

describe("AgentEscrow (v0.1 expanded)", function () {
  let escrow;
  let customer;
  let agent;
  let stranger;

  beforeEach(async function () {
    [customer, agent, stranger] = await ethers.getSigners();
    const AgentEscrow = await ethers.getContractFactory("AgentEscrow");
    escrow = await AgentEscrow.deploy();
    await escrow.waitForDeployment();
  });

  function taskId(messageId) {
    return ethers.keccak256(
      ethers.solidityPacked(["string", "address"], [messageId, customer.address])
    );
  }

  async function createTask(messageId = "msg-001", opts = {}) {
    const budget = opts.budget ?? ethers.parseEther("1.0");
    const timeout = opts.timeout ?? Math.floor(Date.now() / 1000) + 3600;
    const royaltyAgent = opts.royaltyAgent ?? 10;
    const slashStake = opts.slashStake ?? 20;
    const token = opts.token ?? ZERO_ADDR;
    const value = token === ZERO_ADDR ? budget : 0;
    const tx = await escrow.connect(customer).createTask(
      messageId, budget, token, timeout, royaltyAgent, slashStake,
      { value }
    );
    await tx.wait();
    return taskId(messageId);
  }

  describe("createTask", function () {
    it("creates a task with budget and royalty split", async function () {
      const id = await createTask();
      const task = await escrow.getTask(id);
      expect(task.customer).to.equal(customer.address);
      expect(task.budget).to.equal(ethers.parseEther("1.0"));
      expect(task.royaltyAgent).to.equal(10);
      expect(task.royaltyHuman).to.equal(90);
      expect(task.status).to.equal(0); // Initiated
    });

    it("rejects zero budget", async function () {
      await expect(
        escrow.connect(customer).createTask(
          "msg-zero", 0, ZERO_ADDR, Math.floor(Date.now() / 1000) + 3600, 10, 20
        )
      ).to.be.revertedWith("Budget must be > 0");
    });

    it("rejects past timeout", async function () {
      await expect(
        escrow.connect(customer).createTask(
          "msg-past", ethers.parseEther("1.0"), ZERO_ADDR,
          Math.floor(Date.now() / 1000) - 10, 10, 20,
          { value: ethers.parseEther("1.0") }
        )
      ).to.be.revertedWith("Timeout must be future");
    });

    it("rejects royalty > 100", async function () {
      await expect(
        escrow.connect(customer).createTask(
          "msg-r", ethers.parseEther("1.0"), ZERO_ADDR,
          Math.floor(Date.now() / 1000) + 3600, 101, 20,
          { value: ethers.parseEther("1.0") }
        )
      ).to.be.revertedWith("Royalty <=100");
    });
  });

  describe("full lifecycle", function () {
    it("create -> accept -> submit -> release with royalty split", async function () {
      const id = await createTask();

      // Accept by agent
      await expect(escrow.connect(agent).acceptTask(id))
        .to.emit(escrow, "TaskAccepted").withArgs(id, agent.address);
      let task = await escrow.getTask(id);
      expect(task.agent).to.equal(agent.address);
      expect(task.status).to.equal(1); // Accepted

      // Submit result
      await expect(escrow.connect(agent).submitResult(id, "0xabc123"))
        .to.emit(escrow, "ResultSubmitted").withArgs(id, "0xabc123");
      task = await escrow.getTask(id);
      expect(task.executionHash).to.equal("0xabc123");
      expect(task.status).to.equal(2); // Completed

      // Verify and release: 10% agent, 90% customer
      const agentBefore = await ethers.provider.getBalance(agent.address);
      const customerBefore = await ethers.provider.getBalance(customer.address);
      await expect(escrow.connect(customer).verifyAndRelease(id, true))
        .to.emit(escrow, "EscrowReleased");

      const agentAfter = await ethers.provider.getBalance(agent.address);
      const customerAfter = await ethers.provider.getBalance(customer.address);
      // agent receives 0.1 ETH
      expect(agentAfter - agentBefore).to.be.closeTo(ethers.parseEther("0.1"), ethers.parseEther("0.01"));
      // customer receives 0.9 ETH (minus gas)
      expect(customerAfter).to.be.gt(customerBefore);

      task = await escrow.getTask(id);
      expect(task.status).to.equal(3); // Released
      expect(await escrow.getReputation(agent.address)).to.equal(1);
    });

    it("refunds customer on failure and penalizes reputation", async function () {
      const id = await createTask();
      await escrow.connect(agent).acceptTask(id);
      await escrow.connect(agent).submitResult(id, "0xbad");
      await escrow.connect(customer).verifyAndRelease(id, false);

      const task = await escrow.getTask(id);
      expect(task.status).to.equal(5); // Refunded
      expect(await escrow.getReputation(agent.address)).to.equal(-1);
    });

    it("only the assigned agent can submit", async function () {
      const id = await createTask();
      await escrow.connect(agent).acceptTask(id);
      await expect(
        escrow.connect(stranger).submitResult(id, "0xevil")
      ).to.be.revertedWith("Only assigned agent");
    });

    it("only customer can release", async function () {
      const id = await createTask();
      await escrow.connect(agent).acceptTask(id);
      await escrow.connect(agent).submitResult(id, "0xabc");
      await expect(
        escrow.connect(stranger).verifyAndRelease(id, true)
      ).to.be.revertedWith("Only customer");
    });
  });

  describe("slashOnTimeout", function () {
    it("slashes agent stake and refunds principal after deadline", async function () {
      const budget = ethers.parseEther("1.0");
      const timeout = Math.floor(Date.now() / 1000) + 60;
      const id = await createTask("msg-timeout", { budget, timeout, slashStake: 20 });

      await escrow.connect(agent).acceptTask(id);

      // Fast-forward past deadline
      await ethers.provider.send("evm_increaseTime", [120]);
      await ethers.provider.send("evm_mine", []);

      const customerBefore = await ethers.provider.getBalance(customer.address);
      await expect(escrow.connect(stranger).slashOnTimeout(id))
        .to.emit(escrow, "TaskSlashed");

      const customerAfter = await ethers.provider.getBalance(customer.address);
      // customer gets back 0.8 ETH (20% slashed/burned)
      expect(customerAfter - customerBefore).to.be.closeTo(ethers.parseEther("0.8"), ethers.parseEther("0.01"));

      const task = await escrow.getTask(id);
      expect(task.status).to.equal(4); // Slashed
      expect(await escrow.getReputation(agent.address)).to.equal(-5);
    });

    it("cannot slash before deadline", async function () {
      const id = await createTask("msg-early");
      await escrow.connect(agent).acceptTask(id);
      await expect(
        escrow.connect(stranger).slashOnTimeout(id)
      ).to.be.revertedWith("Deadline not reached");
    });
  });
});
