// Network profiles. Only TRON networks exist here by design: the service has no
// code path for Ethereum, BSC, Solana or any other chain.
//
// genesisBlockId is used at startup to prove that TRON_RPC_URL really points at
// the expected TRON network (a mis-configured testnet/mainnet URL is refused).
// If TRON ever changes how block 0 is reported by the API, set
// EXPECTED_GENESIS_BLOCK_ID after verifying it on Tronscan.
//
// referenceUsdtContract is NOT used to send funds. The contract that is used is
// always USDT_CONTRACT_ADDRESS from the environment. The reference is only a
// guard: if the configured address differs from the widely published official
// Tether contract for that network, startup is refused unless the operator
// explicitly sets USDT_ALLOW_NONSTANDARD_CONTRACT=true.

export const NETWORKS = Object.freeze({
  mainnet: Object.freeze({
    name: 'mainnet',
    label: 'TRON',
    genesisBlockId: '00000000000000001ebf88508a03865c71d452e25f4d51194196a1d22b6653dc',
    tronscanTxUrl: 'https://tronscan.org/#/transaction/',
    tronscanAddressUrl: 'https://tronscan.org/#/address/',
    referenceUsdtContract: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t',
  }),
  // Testnet profile for dry runs / end-to-end testing without real funds.
  nile: Object.freeze({
    name: 'nile',
    label: 'TRON Nile testnet',
    genesisBlockId: '0000000000000000d698d4192c56cb6be724a558448e2684802de4d6cd8690dc',
    tronscanTxUrl: 'https://nile.tronscan.org/#/transaction/',
    tronscanAddressUrl: 'https://nile.tronscan.org/#/address/',
    referenceUsdtContract: 'TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf',
  }),
});
