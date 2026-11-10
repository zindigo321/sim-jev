"""CPU tensor behavior, independent of expensive backbone inference."""
import unittest
try:
    import torch
    from sim_jev.qwen_pointer_runtime import SharedPointerHead
except ImportError:
    torch = None


@unittest.skipUnless(torch is not None, 'tensor tests require the existing cgfedllm interpreter')
class HeadTests(unittest.TestCase):
    def test_shared_scores_permute_with_candidates_and_mask_has_zero_probability(self):
        torch.manual_seed(7)
        head=SharedPointerHead(24,36)
        c,q=torch.randn(4,24),torch.randn(36)
        perm=torch.tensor([2,0,3,1])
        self.assertTrue(torch.allclose(head(c,q)[perm],head(c[perm],q)))
        mask=torch.tensor([True,False,True,True])
        logits=head(c,q,mask)
        self.assertEqual(torch.softmax(logits,-1)[1].item(),0.)
        with self.assertRaises(ValueError):head(c,q,torch.zeros(4,dtype=torch.bool))

    def test_soft_target_update_reaches_both_projections_and_norms(self):
        torch.manual_seed(7)
        head=SharedPointerHead(24,36)
        c,q=torch.randn(4,24),torch.randn(36)
        loss=-(torch.tensor([.7,.1,.1,.1])*torch.log_softmax(head(c,q),-1)).sum()
        loss.backward()
        for name,p in head.named_parameters():
            self.assertIsNotNone(p.grad,name)
            self.assertGreater(p.grad.abs().sum().item(),0,name)


if __name__=='__main__':unittest.main()
