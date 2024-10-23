import json
import os
from datetime import datetime, timedelta, timezone

import anthropic
from ape import Contract, chain
from ape.contracts import ContractInstance
from ape.types import AddressType
from ape_ethereum import multicall
from ape_tokens import tokens
from ape_tokens.managers import ERC20
from evmchains import get_chain_meta
from pydantic import BaseModel, Field
from silverback import SilverbackBot
from uniswap_sdk import Plan, UniversalRouter
from uniswap_sdk.packages import V2, get_contract_instance
from uniswap_sdk.universal_router import Constants as URConst

BACKUP = AddressType(os.environ.get("BACKUP_ADDRESS"))
PROFIT_THRESHOLD = float(os.environ.get("PROFIT_THRESHOLD", 1000.0))

bot = SilverbackBot()
router = UniversalRouter()

WETH = tokens.get("WETH") or os.environ["WETH_ADDRESS"]
UNI_V2_FACTORY = get_contract_instance(
    V2.UniswapV2Factory,
    get_chain_meta(bot.identifier.ecosystem, bot.identifier.network).chainId,
)

AI_MODEL_NAME = "claude-3-haiku-20240307"
SYSTEM_PROMPT = """For the purposes of this conversation, I'd like you to respond with an floating point number between 0 and 1. Your background is that you are a really smart meme trader that is an expert at knowing when a meme is good or bad. If I share with you any meme name and shorthand symbol for that meme, I want you to tell me how strongly you feel it is memetic and will likely get attention. I want you to be conservative in your estimates, with 0.05 as your average guess, using 0 if you don't like it at all, and only using a number close to 1 if you are extremely confident that a meme is likely to go viral. I do not want you to provide any other commentary whatsoever.

I will provide examples in the future using a JSON format that looks like the following:
```json
{
    "name": "<meme name>",
    "symbol": "<shorthand symbol for meme>"
}
```"""


class Buy(BaseModel):
    price: float
    amount: int  # amount bought
    bought_at: datetime = Field(default_factory=datetime.now)
    token_address: AddressType
    pair_address: AddressType

    @property
    def token(self) -> ContractInstance:
        return Contract(self.token_address, contract_type=ERC20)

    @property
    def pair(self) -> ContractInstance:
        return V2.UniswapV2Pair.at(self.pair_address)


@bot.on_startup()
async def load_state(_):
    bot.state.ai = anthropic.AsyncAnthropic()
    bot.state.buys = {}


@bot.on_(UNI_V2_FACTORY.PairCreated)
async def buy(log):
    if log.token0 != WETH:
        # Not using WETH as base token
        return

    token = Contract(log.token1, contract_type=ERC20)
    symbol = token.symbol()
    decimals = token.decimals()

    pair = V2.UniswapV2Pair.at(log.pair)
    reserve0, reserve1, _ = pair.getReserves()
    current_price = reserve0 / reserve1

    response = await bot.state.ai.messages.create(
        model=AI_MODEL_NAME,
        max_tokens=1000,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[
            dict(
                role="user",
                content=json.dumps(dict(name=token.name(), symbol=symbol)),
            ),
        ],
    )

    if (ratio := float(response.content[0].text)) == 0.0:
        # Meme is a dud, drop it
        return

    # print(f"[{symbol}] ({token.address}) @ {current_price}")
    if not bot.signer:
        # Monitoring mode, please add a signer to actually trade
        bot.state.buys[symbol] = Buy(  # simulate a buy
            price=current_price,
            amount=10**decimals,
            token_address=token.address,
            pair_address=pair.address,
        )
        return

    purchase_amount = int(ratio * bot.signer.balance)
    plan = (
        Plan()
        .wrap_eth(URConst.ADDRESS_THIS, purchase_amount)
        .v2_swap_exact_in(
            URConst.MSG_SENDER,
            purchase_amount,
            int(0.995 * current_price * purchase_amount),
            [WETH, token],
            False,
        )
    )

    deadline = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())
    router.execute(
        plan,
        deadline=deadline,
        sender=bot.signer,
        value=purchase_amount,
    )
    token_balance = token.balanceOf(bot.signer)
    buy_price = token_balance / (10**decimals) / purchase_amount

    print(
        f"Bought {token_balance / 10 ** decimals} {symbol} "
        f"@ {buy_price:.18f} {symbol}/ETH"
    )
    bot.state.buys[symbol] = Buy(  # simulate a buy
        price=buy_price,
        amount=purchase_amount,
        token_address=token.address,
        pair_address=pair.address,
    )

    return {symbol: buy_price}


@bot.on_(chain.blocks)
async def pnl(blk):
    if len(bot.state.buys) == 0:
        print("No buys yet")
        return

    call = multicall.Call()
    for symbol, buy in bot.state.buys.items():
        call.add(buy.pair.getReserves)

    current_prices = {}
    tokens_to_swap = {}
    tokens_to_drop = []
    for symbol, (reserve0, reserve1, _) in zip(bot.state.buys, call()):
        current_price = current_prices[symbol] = reserve0 / reserve1
        # print(f"[{symbol}] Current price is {current_price}")

        if buy := bot.state.buys.get(symbol):
            pnl = (current_price - buy.price) / buy.price
            print(f"[{symbol}] Profit or Loss: {100.0 * pnl:0.2f}%")

            if pnl > 1000.0:  # steel hands
                tokens_to_swap[symbol] = buy.token

            elif pnl < 99.0:  # it was a rug, drop it
                tokens_to_drop.append(symbol)

    for symbol in tokens_to_drop:
        bot.state.buys.pop(symbol)

    if not bot.signer or not tokens_to_swap:
        return current_prices

    plan = Plan()
    for symbol, token in tokens_to_swap.items():
        # NOTE: Pop this out so we don't track it anymore
        #       (either it succeeds or it was was a rug/can't sell)
        buy = bot.state.buys.pop(symbol)

        if token.balanceOf(bot.signer) == 0:
            # Assume we are in fork simulation mode
            # buy.token.balanceOf[bot.signer] = buy.amount
            print(f"No balance detected for {symbol}")
            continue

        token_balance = token.balanceOf(bot.signer)
        if buy.pair.allowance(bot.signer, router.contract) < token_balance:
            # TODO: Use Permit2 approvals instead
            buy.pair.approve(
                router.contract,
                2**256 - 1,
                sender=bot.signer,
                required_confirmations=0,
            )

        plan = plan.approve_erc20(token, buy.pair).v2_swap_exact_in(
            URConst.ADDRESS_THIS,
            token_balance,
            # NOTE: Pop this out so we don't report it below
            int(0.95 * token_balance / current_prices.pop(symbol)),
            [token, WETH],
            True,
        )
        print(f"Selling {token_balance / 10 ** token.decimals()} {symbol}")

    plan = plan.unwrap_weth(URConst.MSG_SENDER, 0)
    deadline = blk.timestamp + int(timedelta(minutes=2).total_seconds())

    starting_balance = bot.signer.balance
    tx = router.execute(
        plan,
        deadline=deadline,
        sender=bot.signer,
    )
    if tx.failed:
        print(tx.show_trace())
        raise tx.error

    profit = bot.signer.balance - starting_balance
    print(f"Made {profit} ETH from selling {len(tokens_to_swap)} tokens")

    return current_prices


@bot.on_shutdown()
async def transfer_all_to_backup():
    if not bot.signer:
        return  # Paper trading, no backup required

    elif not BACKUP:
        print("No backup enabled, add `BACKUP_ADDRESS=<your address>` to enable")
        return

    for symbol, buy in bot.state.buys.items():
        if (token_balance := buy.token.balanceOf(bot.signer)) == 0:
            continue

        decimals = buy.token.decimals()
        print(f"Transferring {token_balance / (10 ** decimals)} {symbol} to BACKUP")
        buy.token.transfer(
            BACKUP,
            token_balance,
            sender=bot.signer,
            required_confirmations=0,
        )
