// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title AgentEscrow
 * @dev Escrow contract for ACP agent contracts over a global mesh network.
 *
 * v0.1 (expanded spec):
 *   - ERC-20 token support (default USDC) with native-Ether fallback.
 *   - Two-phase lifecycle: Created -> Accepted -> Submitted -> Released/Slashed.
 *   - slashOnTimeout() — slashes agent stake and refunds principal when deadline missed.
 *   - reputation mapping: agent -> accumulated score (slashes decrement it).
 *   - royalty split: human_principal (100 - royaltyAgent) / agent_executor (%).
 */
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

contract AgentEscrow {
    // ── State ──────────────────────────────────────────────
    enum TaskStatus { Initiated, Accepted, Completed, Released, Slashed, Refunded }

    struct Task {
        address customer;         // principal / human
        address agent;            // executor (set on accept)
        uint256 budget;           // amount locked in escrow
        address token;            // ERC-20 address, address(0) = native ETH
        uint256 timeout;          // unix deadline
        string messageId;         // ACP contract message id
        string executionHash;     // sandbox proof hash
        uint8 royaltyHuman;       // share (0..100) for customer
        uint8 royaltyAgent;       // share (0..100) for executor
        uint8 slashStake;         // % of budget slashed on timeout
        TaskStatus status;
        uint256 createdAt;
    }

    mapping(bytes32 => Task) public tasks;
    mapping(address => int256) public reputation;   // agent -> reputation score
    mapping(address => uint256) public nativeBalances;  // native ETH locked by token(0) deposits

    // ── Events ─────────────────────────────────────────────
    event TaskCreated(
        bytes32 indexed taskId, address indexed customer, uint256 budget, address token, string messageId
    );
    event TaskAccepted(bytes32 indexed taskId, address indexed agent);
    event ResultSubmitted(bytes32 indexed taskId, string executionHash);
    event EscrowReleased(bytes32 indexed taskId, address indexed agent, uint256 agentShare, uint256 principalShare);
    event TaskSlashed(bytes32 indexed taskId, address indexed agent, uint256 amount);
    event EscrowRefunded(bytes32 indexed taskId, address indexed customer, uint256 amount);

    constructor() {}

    // ── Internal ────────────────────────────────────────────
    function _computeTaskId(string calldata messageId, address depositor) private pure returns (bytes32) {
        return keccak256(abi.encodePacked(messageId, depositor));
    }

    function _transferOut(address token, address to, uint256 amount) private {
        if (token == address(0)) {
            (bool ok, ) = to.call{value: amount}("");
            require(ok, "Native transfer failed");
        } else {
            require(IERC20(token).transfer(to, amount), "Token transfer failed");
        }
    }

    // ── Create (deposit funds) ─────────────────────────────
    /**
     * @notice Create escrow task and lock budget.
     * @param _messageId    ACP contract message id
     * @param _budget       locked budget (>= 0.01 USDC enforced off-chain)
     * @param _token        ERC-20 token address (0x0 = native ETH)
     * @param _timeout      absolute unix deadline
     * @param _royaltyAgent executor share 0..100 (%)
     * @param _slashStake   % slashed from budget on timeout
     */
    function createTask(
        string calldata _messageId,
        uint256 _budget,
        address _token,
        uint256 _timeout,
        uint8 _royaltyAgent,
        uint256 _slashStake
    ) external payable returns (bytes32 taskId) {
        require(_budget > 0, "Budget must be > 0");
        require(_timeout > block.timestamp, "Timeout must be future");
        require(_royaltyAgent <= 100, "Royalty <=100");
        require(_slashStake <= 100, "Slashstake <=100");

        if (_token == address(0)) {
            require(msg.value == _budget, "ETH mismatch");
            nativeBalances[msg.sender] += _budget;
        } else {
            require(IERC20(_token).transferFrom(msg.sender, address(this), _budget), "ERC20 transfer failed");
        }

        taskId = _computeTaskId(_messageId, msg.sender);
        tasks[taskId] = Task({
            customer: msg.sender,
            budget: _budget,
            token: _token,
            timeout: _timeout,
            messageId: _messageId,
            executionHash: "",
            royaltyHuman: uint8(100 - _royaltyAgent),
            royaltyAgent: _royaltyAgent,
            slashStake: uint8(_slashStake),
            status: TaskStatus.Initiated,
            createdAt: block.timestamp,
            agent: address(0)
        });

        emit TaskCreated(taskId, msg.sender, _budget, _token, _messageId);
    }

    // ── Accept ─────────────────────────────────────────────
    function acceptTask(bytes32 _taskId) external {
        Task storage task = tasks[_taskId];
        require(task.status == TaskStatus.Initiated, "Not initiated");
        task.agent = msg.sender;
        task.status = TaskStatus.Accepted;
        emit TaskAccepted(_taskId, msg.sender);
    }

    // ── Submit ─────────────────────────────────────────────
    function submitResult(bytes32 _taskId, string calldata _executionHash) external {
        Task storage task = tasks[_taskId];
        require(task.status == TaskStatus.Accepted, "Not accepted");
        require(task.agent == msg.sender, "Only assigned agent");
        task.executionHash = _executionHash;
        task.status = TaskStatus.Completed;
        emit ResultSubmitted(_taskId, _executionHash);
    }

    // ── Verify + release ───────────────────────────────────
    /**
     * @notice Customer (or verifier) releases funds with royalty split; updates reputation.
     */
    function verifyAndRelease(bytes32 _taskId, bool _success) external {
        Task storage task = tasks[_taskId];
        require(msg.sender == task.customer, "Only customer");
        require(task.status == TaskStatus.Completed, "Not completed");

        if (_success) {
            uint256 agentShare = (task.budget * task.royaltyAgent) / 100;
            uint256 principalShare = task.budget - agentShare;
            _transferOut(task.token, task.agent, agentShare);
            _transferOut(task.token, task.customer, principalShare);
            reputation[task.agent] += 1;
            task.status = TaskStatus.Released;
            emit EscrowReleased(_taskId, task.agent, agentShare, principalShare);
        } else {
            _transferOut(task.token, task.customer, task.budget);
            reputation[task.agent] -= 1;
            task.status = TaskStatus.Refunded;
            emit EscrowRefunded(_taskId, task.customer, task.budget);
        }
    }

    // ── Slash on timeout ───────────────────────────────────
    /**
     * @notice Slash agent stake and refund principal when deadline missed.
     */
    function slashOnTimeout(bytes32 _taskId) external {
        Task storage task = tasks[_taskId];
        require(
            task.status == TaskStatus.Initiated || task.status == TaskStatus.Accepted,
            "Not actionable"
        );
        require(block.timestamp > task.timeout, "Deadline not reached");

        uint256 slashAmount = (task.budget * task.slashStake) / 100;
        uint256 refundAmount = task.budget - slashAmount;
        _transferOut(task.token, task.customer, refundAmount);
        if (task.agent != address(0)) reputation[task.agent] -= 5;
        task.status = TaskStatus.Slashed;
        emit TaskSlashed(_taskId, task.agent, slashAmount);
    }

    // ── Views ──────────────────────────────────────────────
    function getTask(bytes32 _taskId) external view returns (Task memory) {
        return tasks[_taskId];
    }

    function getReputation(address _agent) external view returns (int256) {
        return reputation[_agent];
    }
}