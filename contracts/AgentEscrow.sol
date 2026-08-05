// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title AgentEscrow
 * @dev Simple escrow contract for ACP agent contracts.
 *      MVP version — no ZK proofs, no complex reputation.
 */
contract AgentEscrow {
    enum TaskStatus { Created, Submitted, Released, Refunded, Disputed }

    struct Task {
        address customer;
        address agent;
        uint256 budget;
        string token;
        uint256 timeout;
        string messageId;
        string executionHash;
        TaskStatus status;
        uint256 createdAt;
    }

    mapping(bytes32 => Task) public tasks;
    mapping(address => uint256) public balances;

    event TaskCreated(
        bytes32 indexed taskId,
        address indexed customer,
        address indexed agent,
        uint256 budget,
        string token,
        string messageId
    );

    event ResultSubmitted(
        bytes32 indexed taskId,
        string executionHash
    );

    event Released(
        bytes32 indexed taskId,
        address indexed agent,
        uint256 amount
    );

    event Refunded(
        bytes32 indexed taskId,
        address indexed customer,
        uint256 amount
    );

    /**
     * @notice Create a new escrow task.
     * @param _messageId The contract message ID.
     * @param _budget The budget amount.
     * @param _token The token type (USDC or ETH).
     * @param _timeout Unix timestamp for timeout.
     */
    function createTask(
        string calldata _messageId,
        uint256 _budget,
        string calldata _token,
        uint256 _timeout
    ) external payable returns (bytes32 taskId) {
        require(_budget > 0, "Budget must be > 0");
        require(msg.value == _budget || (keccak256(abi.encodePacked(_token)) == keccak256(abi.encodePacked("USDC"))), "Budget mismatch");

        taskId = keccak256(abi.encodePacked(_messageId, block.timestamp));
        tasks[taskId] = Task({
            customer: msg.sender,
            agent: address(0),
            budget: _budget,
            token: _token,
            timeout: _timeout,
            messageId: _messageId,
            executionHash: "",
            status: TaskStatus.Created,
            createdAt: block.timestamp
        });

        balances[address(this)] += _budget;

        emit TaskCreated(taskId, msg.sender, address(0), _budget, _token, _messageId);
    }

    /**
     * @notice Submit execution result as the agent.
     * @param _taskId The task ID.
     * @param _executionHash SHA256 of the execution log.
     */
    function submitResult(
        bytes32 _taskId,
        string calldata _executionHash
    ) external {
        Task storage task = tasks[_taskId];
        require(task.status == TaskStatus.Created, "Task not in Created state");
        require(bytes(task.executionHash).length == 0, "Result already submitted");

        task.agent = msg.sender;
        task.executionHash = _executionHash;
        task.status = TaskStatus.Submitted;

        emit ResultSubmitted(_taskId, _executionHash);
    }

    /**
     * @notice Verify and release funds to the agent (by customer or automated verifier).
     * @param _taskId The task ID.
     * @param _success Whether execution was successful.
     */
    function verifyAndRelease(
        bytes32 _taskId,
        bool _success
    ) external {
        Task storage task = tasks[_taskId];
        require(task.status == TaskStatus.Submitted, "Task not in Submitted state");
        require(msg.sender == task.customer || msg.sender == task.agent, "Not authorized");

        if (_success) {
            // Royalty split: 90% customer, 10% agent (for MVP, agent gets full budget)
            // In production, this would split according to the contract terms
            uint256 agentAmount = (task.budget * 10) / 100;
            uint256 customerAmount = task.budget - agentAmount;

            // Transfer agent portion
            (bool sent1, ) = task.agent.call{value: agentAmount}("");
            require(sent1, "Agent transfer failed");

            // Customer keeps their portion (already paid)
            task.status = TaskStatus.Released;

            emit Released(_taskId, task.agent, agentAmount);
        } else {
            // Refund to customer
            (bool sent2, ) = task.customer.call{value: task.budget}("");
            require(sent2, "Refund failed");

            task.status = TaskStatus.Refunded;

            emit Refunded(_taskId, task.customer, task.budget);
        }
    }

    /**
     * @notice Get task details.
     */
    function getTask(bytes32 _taskId) external view returns (Task memory) {
        return tasks[_taskId];
    }

    /**
     * @notice Get contract balance.
     */
    function getBalance() external view returns (uint256) {
        return address(this).balance;
    }
}
