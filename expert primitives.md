# 专家操作列表 (Expert Primitives)

来源: https://dataplant2.netlify.app/ 的 actions.json (expert_operations),共 10 个。

专家操作是完整的长序列技能,不再拆分为原子操作;typical_length 表示典型动作序列长度。

## E_001 叠衣服 FoldCloth

logic0: Fold the <obj_a>.

        叠 <obj_a>。

典型长度: 57

适用对象:
  obj_a: T_Shirt / Shirt / Sweater / Jacket / Coat / Trousers / Shorts / Dress / Skirt / Socks / Underwear / Bra / Scarf / Tie

## E_002 衣服挂上支架 HangCloth

logic0: Hang the <obj_a> on the <obj_b>.

        把 <obj_a> 挂在 <obj_b> 上。

典型长度: 45

适用对象:
  obj_a: T_Shirt / Shirt / Sweater / Dress / Trousers / Shorts / Socks / Underwear / Bra / Bedsheet / Quilt_Cover / Towel / Coat / Jacket
  obj_b: Hanger

## E_003 叠床品 FoldBedding

logic0: Fold the <obj_a>.

        叠 <obj_a>。

典型长度: 50

适用对象:
  obj_a: Towel / Bedsheet / Quilt_Cover / Pillowcase / Blanket

## E_004 折纸盒 FoldPaper

logic0: Fold the <obj_a>.

        折叠 <obj_a>。

典型长度: 115

适用对象:
  obj_a: Suitcase / Paper

## E_005 撕开 TearOpen

logic0: Tear open the <obj_a>.

        撕开 <obj_a>。

典型长度: 7

适用对象:
  obj_a: Frozen_Food / Jar / Carton / Suitcase / Mask

## E_006 缠绕收纳 CableWrap

logic0: Wrap up the <obj_a>.

        缠绕收纳 <obj_a>。

典型长度: 34

适用对象:
  obj_a: Cable / Rope

## E_007 铺床 MakeBed

logic0: Make the bed with the <obj_a>.

        用 <obj_a> 铺床。

典型长度: 59

适用对象:
  obj_a: Bedsheet / Quilt / Pillow / Blanket

## E_008 开盒 OpenBox

logic0: Open the <obj_a>.

        打开 <obj_a>。

典型长度: 2

适用对象:
  obj_a: Container / Jar / Blocks / Puzzle / Suitcase / Mask / Toilet_Paper

## E_009 合盒 CloseBox

logic0: Close the <obj_a>.

        合上 <obj_a>。

典型长度: 2

适用对象:
  obj_a: Container / Jar / Blocks / Puzzle / Suitcase

## E_010 拉开拉链 Unzip

logic0: Unzip the <obj_a>.

        拉开 <obj_a> 的拉链。

典型长度: 2

适用对象:
  obj_a: Jacket / Coat / Dress / Trousers / Backpack / Briefcase / Handbag / Suitcase
